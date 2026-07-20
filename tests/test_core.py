#!/usr/bin/env python3
"""Pueo test suite — covers logic exercisable without external services."""

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

    def test_log_remote_path_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.LOG_REMOTE_PATH == "/config/home-assistant.log"

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


# ── DiagnosticsReport schema ─────────────────────────────────────────────────────


class TestDiagnosticsReport:
    def test_valid_clear_report(self):
        from ha_agent_core import DiagnosticsReport

        report = DiagnosticsReport(
            is_valid=True,
            severity="NONE",
            identified_issues=[],
            recommended_fix_yaml=None,
        )
        assert report.is_valid
        assert report.recommended_fix_yaml is None

    def test_invalid_report_with_fix_yaml(self):
        from ha_agent_core import DiagnosticsReport

        report = DiagnosticsReport(
            is_valid=False,
            severity="CRITICAL",
            identified_issues=[
                "Malformed YAML in sensor block",
                "Missing required key: platform",
            ],
            recommended_fix_yaml="sensor:\n  - platform: template\n    sensors: {}\n",
        )
        assert not report.is_valid
        assert report.severity == "CRITICAL"
        assert len(report.identified_issues) == 2
        assert "platform" in report.recommended_fix_yaml

    def test_missing_required_fields_raises(self):
        from ha_agent_core import DiagnosticsReport

        with pytest.raises(ValidationError):
            DiagnosticsReport(is_valid=True)  # severity and identified_issues missing

    def test_json_round_trip(self):
        from ha_agent_core import DiagnosticsReport

        original = DiagnosticsReport(
            is_valid=False,
            severity="LOW",
            identified_issues=["Deprecated format"],
            recommended_fix_yaml=None,
        )
        restored = DiagnosticsReport.model_validate_json(original.model_dump_json())
        assert restored == original


# ── Sandbox engine ───────────────────────────────────────────────────────────────


class TestSandboxEngine:
    def test_sandbox_paths_derived_from_config_remote_path(self):
        import ha_agent_sandbox_engine as e

        config_dir = e.CONFIG_REMOTE_PATH.rsplit("/", 1)[0]
        config_file = e.CONFIG_REMOTE_PATH.rsplit("/", 1)[1]
        assert e.SANDBOX_REMOTE_DIR == f"{config_dir}/.agent_sandbox"
        assert e.SANDBOX_REMOTE_FILE == f"{config_dir}/.agent_sandbox/{config_file}"

    def test_sandbox_paths_update_with_custom_config_path(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"home_assistant": {"config_path": "/custom/path/config.yaml"}})
        )
        importlib.reload(sys.modules["config"])
        import ha_agent_sandbox_engine as e

        importlib.reload(e)
        assert e.SANDBOX_REMOTE_DIR == "/custom/path/.agent_sandbox"
        assert e.SANDBOX_REMOTE_FILE == "/custom/path/.agent_sandbox/config.yaml"


# ── Log monitor ──────────────────────────────────────────────────────────────────


class TestLogMonitor:
    def test_pattern_matches_component_error(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search(
            "2024-01-15 12:00:00 ERROR (MainThread) Component error: light.hue"
        )

    def test_pattern_matches_failed_to_initialize(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search(
            "CRITICAL Failed to initialize integration zwave_js"
        )

    def test_pattern_matches_traceback(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search("ERROR Traceback (most recent call last):")

    def test_pattern_matches_invalid_config(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search(
            "ERROR Invalid config for [sensor]: required key not provided @ data['platform']"
        )

    def test_pattern_ignores_info(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert not CRITICAL_LOG_PATTERN.search(
            "INFO (MainThread) [homeassistant.core] Starting Home Assistant"
        )

    def test_pattern_ignores_warnings(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert not CRITICAL_LOG_PATTERN.search(
            "WARNING [homeassistant.components.sensor] Minor deprecation notice"
        )

    def test_pattern_ignores_debug(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert not CRITICAL_LOG_PATTERN.search("DEBUG Loaded integration: light")

    def test_pattern_matches_error_doing_job(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search("ERROR Error doing job: handle_state_change")

    def test_pattern_requires_trigger_keyword(self):
        from ha_log_monitor import CRITICAL_LOG_PATTERN

        assert not CRITICAL_LOG_PATTERN.search(
            "ERROR Something benign happened with no known trigger keyword"
        )

    def test_log_evaluation_schema(self):
        from ha_log_monitor import LogEvaluation

        result = LogEvaluation(
            is_actionable=True,
            root_cause_summary="Malformed YAML in light integration",
            confidence_score=0.95,
        )
        assert result.is_actionable
        assert result.confidence_score == 0.95

    def test_confidence_threshold_value(self):
        from ha_log_monitor import CONFIDENCE_THRESHOLD

        assert 0.0 < CONFIDENCE_THRESHOLD < 1.0

    def test_confidence_threshold_matches_config(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"log_confidence_threshold": 0.85}})
        )
        importlib.reload(sys.modules["config"])
        import ha_log_monitor

        importlib.reload(ha_log_monitor)
        assert ha_log_monitor.CONFIDENCE_THRESHOLD == 0.85


# ── LogEvaluation schema ──────────────────────────────────────────────────────────


class TestLogEvaluation:
    def test_missing_fields_raises(self):
        from ha_log_monitor import LogEvaluation

        with pytest.raises(ValidationError):
            LogEvaluation(
                is_actionable=True
            )  # root_cause_summary and confidence_score missing

    def test_json_round_trip(self):
        from ha_log_monitor import LogEvaluation

        original = LogEvaluation(
            is_actionable=False,
            root_cause_summary="Z-Wave adapter disconnected",
            confidence_score=0.82,
        )
        restored = LogEvaluation.model_validate_json(original.model_dump_json())
        assert restored == original


# ── ha_agent_advanced SQLite layer ───────────────────────────────────────────────


class TestAdvancedDB:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        return path

    def test_init_creates_state_history_table(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "state_history" in tables

    def test_init_creates_backup_registry_table(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "backup_registry" in tables

    def test_init_is_idempotent(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()

    def test_record_state_memory_inserts_row(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_state_memory("abc123", True, ["issue1"], "test action")
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM state_history").fetchone()[0]
        assert count == 1

    def test_record_state_memory_fields(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_state_memory(
            "deadbeef", False, ["err1", "err2"], "patched"
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT config_hash, is_valid, issues_found, action_taken FROM state_history"
            ).fetchone()
        assert row[0] == "deadbeef"
        assert row[1] == 0  # False stored as integer 0
        assert row[2] == "err1, err2"
        assert row[3] == "patched"

    def test_record_backup_slug_inserts_row(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("slug-abc")
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM backup_registry").fetchone()[0]
        assert count == 1

    def test_record_backup_slug_status_is_active(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("slug-xyz")
        with sqlite3.connect(db_path) as conn:
            status = conn.execute("SELECT status FROM backup_registry").fetchone()[0]
        assert status == "ACTIVE"

    def test_schema_version_table_created(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "schema_version" in tables

    def test_schema_version_is_3_after_init(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 3

    def test_version_unchanged_on_second_init(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 3

    def test_pre_migration_database_upgraded(self, db_path):
        import ha_agent_advanced

        # Simulate a database that existed before migration versioning was added
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE state_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER, config_hash TEXT,
                    is_valid INTEGER, issues_found TEXT, action_taken TEXT
                )
            """
            )
            conn.execute(
                """
                CREATE TABLE backup_registry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER, backup_slug TEXT, status TEXT
                )
            """
            )
            conn.commit()

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 3

    def test_migration_v2_adds_correlation_id_column(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            cols = [
                r[1]
                for r in conn.execute("PRAGMA table_info(state_history)").fetchall()
            ]
        assert "correlation_id" in cols

    def test_record_state_memory_stores_correlation_id(self, db_path):
        import ha_agent_advanced
        from utils.logging import set_correlation_id

        set_correlation_id("test-cid-adv")
        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_state_memory("hash1", True, [], "action")
        with sqlite3.connect(db_path) as conn:
            cid = conn.execute("SELECT correlation_id FROM state_history").fetchone()[0]
        assert cid == "test-cid-adv"


# ── ha_agent_sandbox_engine SQLite layer ─────────────────────────────────────────


class TestSandboxDB:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_sandbox_engine

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_sandbox_engine, "DB_PATH", path)
        return path

    def test_init_creates_state_history_table(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "state_history" in tables

    def test_init_creates_backup_registry_table(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "backup_registry" in tables

    def test_init_is_idempotent(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.init_local_database()

    def test_record_state_memory_inserts_row(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.record_state_memory(
            "abc123", True, ["issue1"], "test action"
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM state_history").fetchone()[0]
        assert count == 1

    def test_record_state_memory_fields(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.record_state_memory(
            "deadbeef", False, ["err1", "err2"], "patched"
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT config_hash, is_valid, issues_found, action_taken FROM state_history"
            ).fetchone()
        assert row[0] == "deadbeef"
        assert row[1] == 0
        assert row[2] == "err1, err2"
        assert row[3] == "patched"

    def test_record_backup_slug_inserts_row(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.record_backup_slug("slug-abc")
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM backup_registry").fetchone()[0]
        assert count == 1

    def test_record_backup_slug_status_is_active(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.record_backup_slug("slug-xyz")
        with sqlite3.connect(db_path) as conn:
            status = conn.execute("SELECT status FROM backup_registry").fetchone()[0]
        assert status == "ACTIVE"

    def test_schema_version_table_created(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "schema_version" in tables

    def test_schema_version_is_2_after_init(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 2

    def test_version_unchanged_on_second_init(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 2

    def test_pre_migration_database_upgraded(self, db_path):
        import ha_agent_sandbox_engine

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE state_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER, config_hash TEXT,
                    is_valid INTEGER, issues_found TEXT, action_taken TEXT
                )
            """
            )
            conn.execute(
                """
                CREATE TABLE backup_registry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER, backup_slug TEXT, status TEXT
                )
            """
            )
            conn.commit()

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 2

    def test_migration_v2_adds_correlation_id_column(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            cols = [
                r[1]
                for r in conn.execute("PRAGMA table_info(state_history)").fetchall()
            ]
        assert "correlation_id" in cols

    def test_record_state_memory_stores_correlation_id(self, db_path):
        import ha_agent_sandbox_engine
        from utils.logging import set_correlation_id

        set_correlation_id("test-cid-sbx")
        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.record_state_memory("hash2", False, ["e"], "action")
        with sqlite3.connect(db_path) as conn:
            cid = conn.execute("SELECT correlation_id FROM state_history").fetchone()[0]
        assert cid == "test-cid-sbx"


# ── Backup slug extraction ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "module_name", ["ha_agent_advanced", "ha_agent_sandbox_engine"]
)
class TestBackupSlugExtraction:
    def test_extract_standard_slug_line(self, module_name):
        mod = importlib.import_module(module_name)
        assert mod._extract_backup_slug("Slug: abc123") == "abc123"

    def test_extract_slug_case_insensitive(self, module_name):
        mod = importlib.import_module(module_name)
        assert mod._extract_backup_slug("SLUG: XYZ789") == "XYZ789"

    def test_extract_multiline_output(self, module_name):
        mod = importlib.import_module(module_name)
        output = "Creating backup...\nSlug: myslug42\nDone."
        assert mod._extract_backup_slug(output) == "myslug42"

    def test_extract_falls_back_to_unknown_slug(self, module_name):
        mod = importlib.import_module(module_name)
        assert mod._extract_backup_slug("No slug info here") == "unknown_slug"


# ── main.py CLI ───────────────────────────────────────────────────────────────────
# Tests call main.main() directly (not via subprocess) so coverage is tracked.


class TestMain:
    def test_missing_config_exits_1(self, monkeypatch, tmp_path, capsys):
        import main as main_module

        monkeypatch.setattr(
            sys, "argv", ["main.py", "--config", str(tmp_path / "nonexistent.yaml")]
        )
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
        assert exc_info.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_invalid_mode_exits_2(self, monkeypatch, tmp_path):
        import main as main_module

        config = tmp_path / "config.yaml"
        config.write_text("")
        monkeypatch.setattr(
            sys, "argv", ["main.py", "--config", str(config), "--mode", "badmode"]
        )
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
        assert exc_info.value.code == 2

    def test_help_exits_0(self, monkeypatch, capsys):
        import main as main_module

        monkeypatch.setattr(sys, "argv", ["main.py", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
        assert exc_info.value.code == 0
        assert "monitor" in capsys.readouterr().out


# ── check_ha_version ─────────────────────────────────────────────────────────────


class TestCheckHaVersion:
    def _ssh(self, stdout: str):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(command_results={"ha core info": (0, stdout, "")})

    def test_versions_match_logs_info(self, isolated_config, caplog):
        isolated_config.write_text(
            yaml.dump({"home_assistant": {"known_version": "2026.7.2"}})
        )
        importlib.reload(sys.modules["config"])
        import ha_agent_core

        importlib.reload(ha_agent_core)
        import logging

        with caplog.at_level(logging.DEBUG):
            asyncio.run(
                ha_agent_core.check_ha_version(
                    ssh_client=self._ssh(
                        "version: 2026.7.2\nversion_latest: 2026.7.2\n"
                    )
                )
            )
        assert any("ha_version_ok" in r.message for r in caplog.records)

    def test_version_changed_logs_warning(self, isolated_config, caplog):
        isolated_config.write_text(
            yaml.dump({"home_assistant": {"known_version": "2026.6.0"}})
        )
        importlib.reload(sys.modules["config"])
        import ha_agent_core

        importlib.reload(ha_agent_core)
        import logging

        with caplog.at_level(logging.DEBUG):
            asyncio.run(
                ha_agent_core.check_ha_version(
                    ssh_client=self._ssh(
                        "version: 2026.7.2\nversion_latest: 2026.7.2\n"
                    )
                )
            )
        assert any("ha_version_changed" in r.message for r in caplog.records)

    def test_no_known_version_skips_check(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import ha_agent_core

        importlib.reload(ha_agent_core)
        ssh = self._ssh("")
        asyncio.run(ha_agent_core.check_ha_version(ssh_client=ssh))
        assert ssh.commands_run == []


# ── utils/prompts.py ─────────────────────────────────────────────────────────────


class TestLoadPrompt:
    def test_loads_known_prompt(self):
        from utils.prompts import load_prompt

        text = load_prompt("diagnose_config")
        assert "Home Assistant" in text

    def test_loads_repair_prompt(self):
        from utils.prompts import load_prompt

        text = load_prompt("diagnose_config_repair")
        assert len(text) > 20

    def test_loads_triage_prompt(self):
        from utils.prompts import load_prompt

        text = load_prompt("triage_log")
        assert "log" in text.lower()

    def test_kwargs_substitution(self, tmp_path, monkeypatch):
        from utils import prompts

        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "test_template.md").write_text(
            "Hello {name}, you have {count} items."
        )
        monkeypatch.setattr(prompts, "_PROMPT_DIR", prompt_dir)
        prompts._cache.clear()

        result = prompts.load_prompt("test_template", name="Alice", count="3")
        assert result == "Hello Alice, you have 3 items."

    def test_no_kwargs_returns_raw_text(self, tmp_path, monkeypatch):
        from utils import prompts

        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "raw.md").write_text("No {placeholders} here... wait.")
        monkeypatch.setattr(prompts, "_PROMPT_DIR", prompt_dir)
        prompts._cache.clear()

        result = prompts.load_prompt("raw")
        assert "{placeholders}" in result

    def test_missing_prompt_raises(self):
        from utils.prompts import load_prompt

        with pytest.raises(FileNotFoundError):
            load_prompt("nonexistent_prompt_xyz")


# ── utils/retry.py ───────────────────────────────────────────────────────────────


class TestAsyncRetry:
    """All tests drive the decorator via asyncio.run() — no external async framework needed."""

    def test_returns_value_on_first_success(self):
        from utils.retry import async_retry

        @async_retry(exceptions=(OSError,))
        async def always_ok():
            return 42

        assert asyncio.run(always_ok()) == 42

    def test_retries_on_matching_exception_then_succeeds(self):
        from utils.retry import async_retry

        calls = []

        @async_retry(max_attempts=3, base_delay=0.0, exceptions=(OSError,))
        async def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise OSError("transient")
            return "ok"

        result = asyncio.run(flaky())
        assert result == "ok"
        assert len(calls) == 2

    def test_non_retryable_exception_passes_through_immediately(self):
        from utils.retry import async_retry

        calls = []

        @async_retry(max_attempts=5, base_delay=0.0, exceptions=(OSError,))
        async def bad():
            calls.append(1)
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            asyncio.run(bad())
        assert len(calls) == 1

    def test_exhausts_max_attempts_and_raises(self):
        from utils.retry import async_retry

        calls = []

        @async_retry(max_attempts=3, base_delay=0.0, exceptions=(OSError,))
        async def always_fail():
            calls.append(1)
            raise OSError("persistent")

        with pytest.raises(OSError):
            asyncio.run(always_fail())
        assert len(calls) == 3

    def test_zero_max_attempts_retries_past_default(self):
        from utils.retry import async_retry

        calls = []

        @async_retry(max_attempts=0, base_delay=0.0, exceptions=(OSError,))
        async def eventually_ok():
            calls.append(1)
            if len(calls) < 10:
                raise OSError("not yet")
            return "done"

        result = asyncio.run(eventually_ok())
        assert result == "done"
        assert len(calls) == 10

    def test_exponential_backoff_grows_between_attempts(self, monkeypatch):
        from utils.retry import async_retry

        delays = []

        async def fake_sleep(secs):
            delays.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        calls = []

        @async_retry(
            max_attempts=4, base_delay=2.0, max_delay=60.0, exceptions=(OSError,)
        )
        async def always_fail():
            calls.append(1)
            raise OSError("err")

        with pytest.raises(OSError):
            asyncio.run(always_fail())

        assert len(delays) == 3
        assert delays[1] > delays[0]
        assert delays[2] > delays[1]

    def test_jitter_keeps_delay_within_25_percent(self, monkeypatch):
        import utils.retry as retry_mod

        # randbelow(51) returning 50 → 50/100 - 0.25 = +0.25 → delay * 1.25
        monkeypatch.setattr(retry_mod.secrets, "randbelow", lambda n: 50)
        captured = []

        async def fake_sleep(secs):
            captured.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        @retry_mod.async_retry(max_attempts=2, base_delay=4.0, exceptions=(OSError,))
        async def fail_once():
            if not captured:
                raise OSError("x")
            return "ok"

        asyncio.run(fail_once())
        assert captured[0] == pytest.approx(4.0 * 1.25)

    def test_ssh_retry_config_keys_exist(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"ssh_retry_attempts": 5, "ssh_retry_base_delay": 1.5}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.SSH_RETRY_ATTEMPTS == 5
        assert config.SSH_RETRY_BASE_DELAY == 1.5

    def test_ssh_retry_config_defaults(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.SSH_RETRY_ATTEMPTS == 3
        assert config.SSH_RETRY_BASE_DELAY == 2.0


# ── utils/rate_limiter.py ────────────────────────────────────────────────────────


class TestDebouncer:
    def test_first_call_triggers(self):
        from utils.rate_limiter import Debouncer

        d = Debouncer(window_seconds=30)
        assert d.record() is True

    def test_second_call_within_window_suppressed(self, monkeypatch):
        from utils.rate_limiter import Debouncer
        import time as time_mod

        now = time_mod.monotonic()
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: now)
        d = Debouncer(window_seconds=30)
        d.record()
        assert d.record() is False

    def test_call_after_window_triggers_again(self, monkeypatch):
        from utils.rate_limiter import Debouncer
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: clock[0])
        d = Debouncer(window_seconds=30)
        d.record()

        clock[0] += 31
        assert d.record() is True

    def test_burst_of_50_produces_one_trigger(self, monkeypatch):
        from utils.rate_limiter import Debouncer
        import time as time_mod

        now = time_mod.monotonic()
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: now)
        d = Debouncer(window_seconds=30)
        results = [d.record() for _ in range(50)]
        assert results.count(True) == 1
        assert results[0] is True


class TestRateLimiter:
    def test_allows_calls_under_limit(self):
        from utils.rate_limiter import RateLimiter

        rl = RateLimiter(max_calls=5, period_seconds=60)
        for _ in range(5):
            rl.check()

    def test_raises_at_limit(self):
        from utils.rate_limiter import RateLimiter, RateLimitExceeded

        rl = RateLimiter(max_calls=3, period_seconds=60)
        for _ in range(3):
            rl.check()
        with pytest.raises(RateLimitExceeded):
            rl.check()

    def test_allows_again_after_period(self, monkeypatch):
        from utils.rate_limiter import RateLimiter
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: clock[0])
        rl = RateLimiter(max_calls=2, period_seconds=60)
        rl.check()
        rl.check()

        clock[0] += 61
        rl.check()

    def test_sliding_window_does_not_count_expired_calls(self, monkeypatch):
        from utils.rate_limiter import RateLimiter
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: clock[0])
        rl = RateLimiter(max_calls=3, period_seconds=60)
        rl.check()
        rl.check()

        clock[0] += 61
        rl.check()
        rl.check()
        rl.check()

    def test_rate_limit_exceeded_is_exception(self):
        from utils.rate_limiter import RateLimitExceeded

        assert issubclass(RateLimitExceeded, Exception)


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


class TestJsonFormatter:
    def _make_record(self, msg: str, **extra):
        import logging as logging_mod

        record = logging_mod.LogRecord(
            name="pueo.test_module",
            level=logging_mod.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_produces_valid_json(self):
        import json
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("something_happened")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_includes_required_fields(self):
        import json
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("config_fetched")
        parsed = json.loads(formatter.format(record))
        assert "timestamp" in parsed
        assert "level" in parsed
        assert "event" in parsed
        assert "module" in parsed

    def test_event_matches_message(self):
        import json
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("backup_created")
        parsed = json.loads(formatter.format(record))
        assert parsed["event"] == "backup_created"

    def test_module_stripped_of_pueo_prefix(self):
        import json
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("x")
        parsed = json.loads(formatter.format(record))
        assert parsed["module"] == "test_module"
        assert not parsed["module"].startswith("pueo.")

    def test_extra_fields_appear_in_output(self):
        import json
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("backup_created")
        record.slug = "abc123"
        record.host = "ha.local"
        parsed = json.loads(formatter.format(record))
        assert parsed["slug"] == "abc123"
        assert parsed["host"] == "ha.local"


class TestStructuredLogger:
    def test_info_calls_underlying_logger(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.logging import StructuredLogger

        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.info("something_happened", key="val")
        inner.log.assert_called_once()
        call_args = inner.log.call_args
        assert call_args[0][1] == "something_happened"
        assert call_args[1]["extra"]["key"] == "val"

    def test_warning_uses_warning_level(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.logging import StructuredLogger

        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.warning("rate_limit_exceeded")
        assert inner.log.call_args[0][0] == logging_mod.WARNING

    def test_error_uses_error_level(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.logging import StructuredLogger

        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.error("ssh_fetch_failed", error="timeout")
        assert inner.log.call_args[0][0] == logging_mod.ERROR


class TestCorrelationId:
    def test_default_is_empty_string(self):
        from utils.logging import get_correlation_id, set_correlation_id

        set_correlation_id("")
        assert get_correlation_id() == ""

    def test_set_and_get_roundtrip(self):
        from utils.logging import get_correlation_id, set_correlation_id

        set_correlation_id("abc-123")
        assert get_correlation_id() == "abc-123"

    def test_correlation_id_included_in_log_extra(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.logging import StructuredLogger, set_correlation_id

        set_correlation_id("repair-uuid-xyz")
        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.info("repair_cycle_started")
        extra = inner.log.call_args[1]["extra"]
        assert extra.get("correlation_id") == "repair-uuid-xyz"

    def test_explicit_correlation_id_not_overwritten(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.logging import StructuredLogger, set_correlation_id

        set_correlation_id("ctx-id")
        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.info("event", correlation_id="explicit-id")
        extra = inner.log.call_args[1]["extra"]
        assert extra["correlation_id"] == "explicit-id"


# ── utils/context.py ─────────────────────────────────────────────────────────────


class TestEstimateTokens:
    def test_empty_string_returns_one(self):
        from utils.context import estimate_tokens

        assert estimate_tokens("") == 1

    def test_four_chars_is_one_token(self):
        from utils.context import estimate_tokens

        assert estimate_tokens("abcd") == 1

    def test_hundred_chars_is_twenty_five_tokens(self):
        from utils.context import estimate_tokens

        assert estimate_tokens("x" * 100) == 25

    def test_scales_with_length(self):
        from utils.context import estimate_tokens

        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("a" * 4000) == 1000


class TestTruncateToBudget:
    def test_short_text_unchanged(self):
        from utils.context import truncate_to_budget

        text = "hello world"
        assert truncate_to_budget(text, 100) == text

    def test_exactly_at_budget_unchanged(self):
        from utils.context import truncate_to_budget

        text = "a" * 400  # 400 chars = 100 tokens exactly
        assert truncate_to_budget(text, 100) == text

    def test_tail_strategy_keeps_end(self):
        from utils.context import truncate_to_budget

        text = "START" + "x" * 400 + "END"
        result = truncate_to_budget(text, 10, strategy="tail")
        assert result.endswith("END")
        assert "START" not in result

    def test_head_strategy_keeps_start(self):
        from utils.context import truncate_to_budget

        text = "START" + "x" * 400 + "END"
        result = truncate_to_budget(text, 10, strategy="head")
        assert result.startswith("START")
        assert "END" not in result

    def test_smart_strategy_includes_separator(self):
        from utils.context import truncate_to_budget

        text = "A" * 2000
        result = truncate_to_budget(text, 100, strategy="smart")
        assert "...[truncated]..." in result

    def test_smart_strategy_keeps_both_ends(self):
        from utils.context import truncate_to_budget

        text = "HEADER" + "x" * 2000 + "FOOTER"
        result = truncate_to_budget(text, 100, strategy="smart")
        assert "HEADER" in result
        assert "FOOTER" in result

    def test_default_strategy_is_tail(self):
        from utils.context import truncate_to_budget

        text = "START" + "z" * 800
        result = truncate_to_budget(text, 10)
        assert "START" not in result
        assert len(result) == 40  # 10 tokens * 4 chars


class TestSlidingWindowLines:
    def test_empty_list_returns_empty(self):
        from utils.context import sliding_window_lines

        assert sliding_window_lines([], 100) == []

    def test_few_lines_all_fit(self):
        from utils.context import sliding_window_lines

        lines = ["line one", "line two", "line three"]
        assert sliding_window_lines(lines, 1000) == lines

    def test_too_many_lines_drops_oldest(self):
        from utils.context import sliding_window_lines

        lines = ["old " * 100 + str(i) for i in range(20)]
        result = sliding_window_lines(lines, 50)
        assert result == lines[len(lines) - len(result) :]

    def test_order_preserved(self):
        from utils.context import sliding_window_lines

        lines = ["alpha", "beta", "gamma"]
        result = sliding_window_lines(lines, 1000)
        assert result == ["alpha", "beta", "gamma"]

    def test_single_line_fits(self):
        from utils.context import sliding_window_lines

        lines = ["short line"]
        assert sliding_window_lines(lines, 100) == lines

    def test_result_fits_within_budget(self):
        from utils.context import sliding_window_lines, estimate_tokens

        lines = ["x" * 100 for _ in range(50)]
        max_tokens = 200
        result = sliding_window_lines(lines, max_tokens)
        total_chars = sum(len(l) + 1 for l in result)
        assert total_chars <= max_tokens * 4


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


# ── utils/yaml_validator.py ──────────────────────────────────────────────────────

_VALID_ORIGINAL = """\
homeassistant:
  name: Home
  latitude: 51.5
  longitude: -0.1
  unit_system: metric
  time_zone: Europe/London

http:
  server_port: 8123

logger:
  default: warning
"""

_VALID_FIX = """\
homeassistant:
  name: Home
  latitude: 51.5
  longitude: -0.1
  unit_system: metric
  time_zone: Europe/London

http:
  server_port: 8124

logger:
  default: info
"""


class TestValidationResult:
    def test_valid_construction(self):
        from utils.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=True, reasons=[])
        assert r.is_safe is True
        assert r.reasons == []

    def test_unsafe_with_reasons(self):
        from utils.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=False, reasons=["missing homeassistant block"])
        assert r.is_safe is False
        assert len(r.reasons) == 1

    def test_reasons_defaults_to_empty_list(self):
        from utils.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=True)
        assert r.reasons == []


class TestValidateProposedFix:
    def test_valid_fix_passes(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, _VALID_FIX)
        assert result.is_safe is True
        assert result.reasons == []

    def test_empty_proposed_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "")
        assert result.is_safe is False
        assert any("empty" in r for r in result.reasons)

    def test_whitespace_only_proposed_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "   \n  ")
        assert result.is_safe is False

    def test_unparseable_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "key: [unclosed")
        assert result.is_safe is False
        assert any("does not parse" in r for r in result.reasons)

    def test_non_mapping_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "- item1\n- item2\n")
        assert result.is_safe is False
        assert any("mapping" in r for r in result.reasons)

    def test_missing_homeassistant_block_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        proposed = "http:\n  server_port: 8123\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("homeassistant" in r for r in result.reasons)

    def test_removed_top_level_key_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        proposed = "homeassistant:\n  name: Home\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("http" in r or "logger" in r for r in result.reasons)

    def test_completely_different_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        proposed = "\n".join([f"key_{i}: value_{i}" for i in range(200)])
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("differs too much" in r for r in result.reasons)

    def test_nearly_identical_fix_passes(self):
        from utils.yaml_validator import validate_proposed_fix

        fix = _VALID_ORIGINAL.replace("warning", "info")
        result = validate_proposed_fix(_VALID_ORIGINAL, fix)
        assert result.is_safe is True

    def test_original_with_bad_yaml_does_not_raise(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix("key: [broken", _VALID_FIX)
        assert isinstance(result.is_safe, bool)

    def test_multiple_violations_reported(self):
        from utils.yaml_validator import validate_proposed_fix

        proposed = "some_new_key:\n  value: x\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert len(result.reasons) >= 2


# ── utils/ssh_client.py (FakeSSHClient) ──────────────────────────────────────────


class TestFakeSSHClient:
    def test_read_file_returns_configured_content(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient(file_contents={"/foo": "bar"})
        assert asyncio.run(c.read_file("/foo")) == "bar"

    def test_read_file_raises_for_unknown_path(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        with pytest.raises(FileNotFoundError):
            asyncio.run(c.read_file("/missing"))

    def test_write_file_records_content(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        asyncio.run(c.write_file("/out", "hello"))
        assert c.written_files["/out"] == "hello"

    def test_run_returns_default_success(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        ec, stdout, stderr = asyncio.run(c.run("anything"))
        assert ec == 0

    def test_run_matches_command_pattern(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient(command_results={"ha core check": (0, "ok", "")})
        ec, stdout, _ = asyncio.run(c.run("ha core check"))
        assert ec == 0
        assert stdout == "ok"

    def test_run_raises_on_check_true_with_nonzero(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient(command_results={"fail_cmd": (1, "", "error")})
        with pytest.raises(RuntimeError):
            asyncio.run(c.run("fail_cmd", check=True))

    def test_run_records_commands(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        asyncio.run(c.run("cmd_one"))
        asyncio.run(c.run("cmd_two"))
        assert "cmd_one" in c.commands_run
        assert "cmd_two" in c.commands_run

    def test_stream_lines_yields_data(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient(stream_data=["line1", "line2", "line3"])

        async def collect():
            return [line async for line in c.stream_lines("tail -F /log")]

        lines = asyncio.run(collect())
        assert lines == ["line1", "line2", "line3"]

    def test_stream_lines_empty(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()

        async def collect():
            return [line async for line in c.stream_lines("tail -F /log")]

        assert asyncio.run(collect()) == []


# ── utils/ollama_client.py (FakeLLMClient) ───────────────────────────────────────


class TestFakeLLMClient:
    def test_chat_returns_configured_json(self):
        from utils.ollama_client import FakeLLMClient

        c = FakeLLMClient('{"key": "value"}')
        result = asyncio.run(c.chat("model", [], {"temperature": 0}, {}))
        assert result["message"]["content"] == '{"key": "value"}'

    def test_chat_records_calls(self):
        from utils.ollama_client import FakeLLMClient

        c = FakeLLMClient("{}")
        asyncio.run(c.chat("mymodel", [{"role": "user", "content": "hi"}], {}, {}))
        assert len(c.calls) == 1
        assert c.calls[0]["model"] == "mymodel"


# ── ha_agent_core pipeline ────────────────────────────────────────────────────────

_SIMPLE_CONFIG = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"


class TestCorePipeline:
    @pytest.fixture
    def ssh(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={"/config/configuration.yaml": _SIMPLE_CONFIG},
            command_results={"ha core check": (0, "", "")},
        )

    @pytest.fixture
    def llm_valid(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_core import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=True,
            severity="NONE",
            identified_issues=[],
            recommended_fix_yaml=None,
        )
        return FakeLLMClient(r.model_dump_json())

    def test_main_valid_config_completes(self, ssh, llm_valid):
        import ha_agent_core

        asyncio.run(ha_agent_core.main(ssh_client=ssh, llm_client=llm_valid))

    def test_main_fetch_config_called(self, ssh, llm_valid):
        import ha_agent_core

        asyncio.run(ha_agent_core.main(ssh_client=ssh, llm_client=llm_valid))
        assert "ha core check" in " ".join(ssh.commands_run)

    def test_main_llm_was_invoked(self, ssh, llm_valid):
        import ha_agent_core

        asyncio.run(ha_agent_core.main(ssh_client=ssh, llm_client=llm_valid))
        assert len(llm_valid.calls) == 1


# ── ha_agent_advanced pipeline ────────────────────────────────────────────────────


class TestAdvancedPipeline:
    @pytest.fixture
    def ssh(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={"/config/configuration.yaml": _SIMPLE_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: adv-slug-1\n", ""),
                "ha core check": (0, "", ""),
            },
        )

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "adv_test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        return path

    @pytest.fixture
    def llm_valid(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_advanced import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=True,
            severity="NONE",
            identified_issues=[],
            recommended_fix_yaml=None,
        )
        return FakeLLMClient(r.model_dump_json())

    @pytest.fixture
    def llm_invalid(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_advanced import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=False,
            severity="LOW",
            identified_issues=["missing key"],
            recommended_fix_yaml=None,
        )
        return FakeLLMClient(r.model_dump_json())

    def test_valid_config_records_state(self, ssh, llm_valid, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_agent_advanced.main(ssh_client=ssh, llm_client=llm_valid))
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM state_history").fetchone()[0]
        assert count == 1

    def test_invalid_config_triggers_backup(self, ssh, llm_invalid, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_agent_advanced.main(ssh_client=ssh, llm_client=llm_invalid))
        assert any("ha backup new" in cmd for cmd in ssh.commands_run)

    def test_invalid_config_backup_slug_recorded(self, ssh, llm_invalid, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_agent_advanced.main(ssh_client=ssh, llm_client=llm_invalid))
        with sqlite3.connect(db_path) as conn:
            slug = conn.execute("SELECT backup_slug FROM backup_registry").fetchone()
        assert slug is not None
        assert slug[0] == "adv-slug-1"

    def test_valid_config_preflight_check_runs(self, ssh, llm_valid, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_agent_advanced.main(ssh_client=ssh, llm_client=llm_valid))
        assert any("ha core check" in cmd for cmd in ssh.commands_run)


# ── ha_agent_sandbox_engine full pipeline ─────────────────────────────────────────

_ORIGINAL_CONFIG = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
_FIXED_CONFIG = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8124\n"
_BAD_FIX = "http:\n  server_port: 8124\n"  # missing homeassistant: block


class TestSandboxPipeline:
    @pytest.fixture
    def ssh_ok(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: sbx-slug-1\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )

    @pytest.fixture
    def ssh_sandbox_fail(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: sbx-slug-2\n", ""),
                "ha core check": (1, "", "config error"),
                "ha core reload": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_sandbox_engine

        path = str(tmp_path / "sbx_test.db")
        monkeypatch.setattr(ha_agent_sandbox_engine, "DB_PATH", path)
        return path

    @pytest.fixture
    def llm_valid(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=True,
            severity="NONE",
            identified_issues=[],
            recommended_fix_yaml=None,
        )
        return FakeLLMClient(r.model_dump_json())

    @pytest.fixture
    def llm_with_fix(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=False,
            severity="LOW",
            identified_issues=["wrong port"],
            recommended_fix_yaml=_FIXED_CONFIG,
        )
        return FakeLLMClient(r.model_dump_json())

    @pytest.fixture
    def llm_bad_fix(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=False,
            severity="LOW",
            identified_issues=["some issue"],
            recommended_fix_yaml=_BAD_FIX,
        )
        return FakeLLMClient(r.model_dump_json())

    @pytest.fixture
    def gate_auto(self):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=True)

    def test_valid_config_no_backup_taken(self, ssh_ok, llm_valid, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(ssh_client=ssh_ok, llm_client=llm_valid)
        )
        assert not any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_valid_config_records_state(self, ssh_ok, llm_valid, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(ssh_client=ssh_ok, llm_client=llm_valid)
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM state_history").fetchone()[0]
        assert count == 1

    def test_repair_path_writes_config(self, ssh_ok, llm_with_fix, db_path, gate_auto):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_with_fix, gate=gate_auto
            )
        )
        assert "/config/configuration.yaml" in ssh_ok.written_files
        assert ssh_ok.written_files["/config/configuration.yaml"] == _FIXED_CONFIG

    def test_repair_path_backup_recorded(
        self, ssh_ok, llm_with_fix, db_path, gate_auto
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_with_fix, gate=gate_auto
            )
        )
        with sqlite3.connect(db_path) as conn:
            slug = conn.execute("SELECT backup_slug FROM backup_registry").fetchone()
        assert slug is not None
        assert slug[0] == "sbx-slug-1"

    def test_sandbox_fail_aborts_atomic_swap(
        self, ssh_sandbox_fail, llm_with_fix, db_path, gate_auto
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_sandbox_fail, llm_client=llm_with_fix, gate=gate_auto
            )
        )
        assert "/config/configuration.yaml" not in ssh_sandbox_fail.written_files

    def test_bad_fix_rejected_before_backup(self, ssh_ok, llm_bad_fix, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(ssh_client=ssh_ok, llm_client=llm_bad_fix)
        )
        assert not any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_bad_fix_records_rejection_in_state(self, ssh_ok, llm_bad_fix, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(ssh_client=ssh_ok, llm_client=llm_bad_fix)
        )
        with sqlite3.connect(db_path) as conn:
            action = conn.execute("SELECT action_taken FROM state_history").fetchone()
        assert action is not None
        assert "rejected" in action[0].lower()

    def test_sandbox_fail_records_state(
        self, ssh_sandbox_fail, llm_with_fix, db_path, gate_auto
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_sandbox_fail, llm_client=llm_with_fix, gate=gate_auto
            )
        )
        with sqlite3.connect(db_path) as conn:
            action = conn.execute("SELECT action_taken FROM state_history").fetchone()
        assert action is not None
        assert "aborted" in action[0].lower()

    def test_repair_path_llm_called_once(
        self, ssh_ok, llm_with_fix, db_path, gate_auto
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_with_fix, gate=gate_auto
            )
        )
        assert len(llm_with_fix.calls) == 1


# ── ha_log_monitor triage with fake LLM ──────────────────────────────────────────


class TestLogMonitorTriage:
    @pytest.fixture
    def llm_actionable(self):
        from utils.ollama_client import FakeLLMClient
        from ha_log_monitor import LogEvaluation

        r = LogEvaluation(
            is_actionable=True,
            root_cause_summary="Malformed YAML in sensor block",
            confidence_score=0.95,
        )
        return FakeLLMClient(r.model_dump_json())

    @pytest.fixture
    def llm_not_actionable(self):
        from utils.ollama_client import FakeLLMClient
        from ha_log_monitor import LogEvaluation

        r = LogEvaluation(
            is_actionable=False,
            root_cause_summary="Transient warning",
            confidence_score=0.1,
        )
        return FakeLLMClient(r.model_dump_json())

    def test_analyze_actionable_line(self, llm_actionable):
        from ha_log_monitor import analyze_log_line_with_ai

        result = asyncio.run(
            analyze_log_line_with_ai(
                ["ERROR Invalid config for sensor"], llm_actionable
            )
        )
        assert result.is_actionable is True
        assert result.confidence_score == 0.95

    def test_analyze_non_actionable_line(self, llm_not_actionable):
        from ha_log_monitor import analyze_log_line_with_ai

        result = asyncio.run(
            analyze_log_line_with_ai(["INFO some benign event"], llm_not_actionable)
        )
        assert result.is_actionable is False

    def test_analyze_calls_llm(self, llm_actionable):
        from ha_log_monitor import analyze_log_line_with_ai

        asyncio.run(analyze_log_line_with_ai(["ERROR Traceback"], llm_actionable))
        assert len(llm_actionable.calls) == 1

    def test_stream_processes_non_matching_lines(self):
        """A stream with no critical lines should complete without triggering triage."""
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from ha_log_monitor import tail_remote_log_stream

        ssh = FakeSSHClient(stream_data=["INFO Starting up", "DEBUG loaded"])
        llm = FakeLLMClient("{}")
        asyncio.run(tail_remote_log_stream(ssh_client=ssh, llm_client=llm))
        assert len(llm.calls) == 0

    def test_stream_triggers_triage_on_critical_line(
        self, llm_not_actionable, monkeypatch
    ):
        """A stream with a CRITICAL line should invoke AI triage."""
        from utils.ssh_client import FakeSSHClient
        from ha_log_monitor import tail_remote_log_stream

        monkeypatch.setattr("ha_log_monitor._debouncer.record", lambda: False)

        ssh = FakeSSHClient(
            stream_data=["ERROR Component error: light.hue broke", "INFO ok"]
        )
        asyncio.run(
            tail_remote_log_stream(ssh_client=ssh, llm_client=llm_not_actionable)
        )
        assert len(llm_not_actionable.calls) == 1


# ── utils/notify.py ──────────────────────────────────────────────────────────────


class TestFakeNotifier:
    def test_send_records_call(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier()
        asyncio.run(n.send("subject", "body", {"notification_id": "x"}))
        assert len(n.sent) == 1
        assert n.sent[0]["subject"] == "subject"

    def test_send_records_payload(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier()
        asyncio.run(
            n.send("s", "b", {"notification_id": "abc", "severity": "CRITICAL"})
        )
        assert n.sent[0]["payload"]["severity"] == "CRITICAL"

    def test_wait_for_approval_returns_true_when_configured(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier(approve=True)
        result = asyncio.run(n.wait_for_approval("any-id"))
        assert result is True

    def test_wait_for_approval_returns_false_when_rejected(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier(approve=False)
        result = asyncio.run(n.wait_for_approval("any-id"))
        assert result is False

    def test_multiple_sends_accumulate(self):
        from utils.notify import FakeNotifier

        n = FakeNotifier()
        asyncio.run(n.send("s1", "b1", {}))
        asyncio.run(n.send("s2", "b2", {}))
        assert len(n.sent) == 2


class TestFileNotifier:
    def test_send_writes_json_file(self, tmp_path):
        from utils.notify import FileNotifier
        import json

        n = FileNotifier(watch_dir=str(tmp_path))
        asyncio.run(n.send("Test subject", "Test body", {"notification_id": "nid-1"}))
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["subject"] == "Test subject"
        assert data["notification_id"] == "nid-1"

    def test_send_creates_watch_dir_if_missing(self, tmp_path):
        from utils.notify import FileNotifier

        new_dir = tmp_path / "hitl"
        n = FileNotifier(watch_dir=str(new_dir))
        asyncio.run(n.send("s", "b", {"notification_id": "x"}))
        assert new_dir.exists()

    def test_wait_for_approval_resolves_approved(self, tmp_path):
        from utils.notify import FileNotifier

        n = FileNotifier(watch_dir=str(tmp_path), poll_interval=0.01)
        (tmp_path / "nid-ok.approved").touch()
        result = asyncio.run(n.wait_for_approval("nid-ok"))
        assert result is True

    def test_wait_for_approval_resolves_rejected(self, tmp_path):
        from utils.notify import FileNotifier

        n = FileNotifier(watch_dir=str(tmp_path), poll_interval=0.01)
        (tmp_path / "nid-no.rejected").touch()
        result = asyncio.run(n.wait_for_approval("nid-no"))
        assert result is False


class TestGetNotifier:
    def test_default_returns_file_notifier(self, tmp_path):
        from utils.notify import FileNotifier, get_notifier

        n = get_notifier("file", notify_watch_dir=str(tmp_path))
        assert isinstance(n, FileNotifier)

    def test_ntfy_returns_ntfy_notifier(self, tmp_path):
        from utils.notify import NtfyNotifier, get_notifier

        n = get_notifier(
            "ntfy", notify_url="https://ntfy.sh/topic", notify_watch_dir=str(tmp_path)
        )
        assert isinstance(n, NtfyNotifier)

    def test_ntfy_wait_for_approval_delegates_to_file(self, tmp_path):
        from utils.notify import NtfyNotifier

        n = NtfyNotifier(url="https://ntfy.sh/topic", watch_dir=str(tmp_path))
        nid = "test-ntfy-approval"
        (tmp_path / f"{nid}.approved").write_text("")
        assert asyncio.run(n.wait_for_approval(nid)) is True

    def test_ntfy_wait_for_rejection_delegates_to_file(self, tmp_path):
        from utils.notify import NtfyNotifier

        n = NtfyNotifier(url="https://ntfy.sh/topic", watch_dir=str(tmp_path))
        nid = "test-ntfy-rejection"
        (tmp_path / f"{nid}.rejected").write_text("")
        assert asyncio.run(n.wait_for_approval(nid)) is False

    def test_webhook_returns_webhook_notifier(self):
        from utils.notify import WebhookNotifier, get_notifier

        n = get_notifier("webhook", notify_url="http://example.com/hook")
        assert isinstance(n, WebhookNotifier)

    def test_unknown_type_defaults_to_file(self, tmp_path):
        from utils.notify import FileNotifier, get_notifier

        n = get_notifier("unknown_type", notify_watch_dir=str(tmp_path))
        assert isinstance(n, FileNotifier)


# ── requires_hitl ────────────────────────────────────────────────────────────────


class TestRequiresHitl:
    def _make_report(self, severity, issues):
        from ha_agent_sandbox_engine import DiagnosticsReport

        return DiagnosticsReport(
            is_valid=False,
            severity=severity,
            identified_issues=issues,
            recommended_fix_yaml=None,
        )

    def test_critical_severity_requires_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("CRITICAL", ["YAML error"])
        assert requires_hitl(report) is True

    def test_low_severity_no_keywords_does_not_require_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["wrong port number"])
        assert requires_hitl(report) is False

    def test_medium_severity_no_keywords_does_not_require_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("MEDIUM", ["deprecated syntax"])
        assert requires_hitl(report) is False

    def test_hacs_keyword_requires_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["HACS integration needs update"])
        assert requires_hitl(report) is True

    def test_hacs_keyword_case_insensitive(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("MEDIUM", ["hacs component failed"])
        assert requires_hitl(report) is True

    def test_database_keyword_requires_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["database schema migration needed"])
        assert requires_hitl(report) is True

    def test_multiple_issues_one_matching_requires_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["wrong port", "database corruption"])
        assert requires_hitl(report) is True

    def test_none_severity_clean_config_does_not_require_hitl(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("NONE", [])
        assert requires_hitl(report) is False

    def test_hitl_always_true_triggers_for_low_severity(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["minor formatting issue"])
        assert requires_hitl(report, hitl_always=True) is True

    def test_hitl_always_true_triggers_for_none_severity(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("NONE", [])
        assert requires_hitl(report, hitl_always=True) is True

    def test_hitl_always_false_preserves_normal_logic(self):
        from ha_agent_sandbox_engine import requires_hitl

        report = self._make_report("LOW", ["minor formatting issue"])
        assert requires_hitl(report, hitl_always=False) is False


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


# ── HITL pipeline gate ────────────────────────────────────────────────────────────


class TestHitlPipelineGate:
    @pytest.fixture
    def ssh_ok(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: hitl-slug-1\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_sandbox_engine

        path = str(tmp_path / "hitl_test.db")
        monkeypatch.setattr(ha_agent_sandbox_engine, "DB_PATH", path)
        return path

    @pytest.fixture
    def llm_critical(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=False,
            severity="CRITICAL",
            identified_issues=["critical YAML error"],
            recommended_fix_yaml=_FIXED_CONFIG,
        )
        return FakeLLMClient(r.model_dump_json())

    @pytest.fixture
    def llm_low_fix(self):
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport

        r = DiagnosticsReport(
            is_valid=False,
            severity="LOW",
            identified_issues=["wrong port"],
            recommended_fix_yaml=_FIXED_CONFIG,
        )
        return FakeLLMClient(r.model_dump_json())

    def test_critical_issue_sends_notification(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        assert len(notifier.sent) == 1

    def test_critical_issue_notification_contains_severity(
        self, ssh_ok, llm_critical, db_path
    ):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        assert "CRITICAL" in notifier.sent[0]["subject"]

    def test_approval_proceeds_to_backup(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        assert any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_rejection_aborts_backup(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=False)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        assert not any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_rejection_records_state(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate
        import sqlite3 as sqlite3_mod

        notifier = FakeNotifier(approve=False)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier, gate=gate
            )
        )
        with sqlite3_mod.connect(db_path) as conn:
            action = conn.execute("SELECT action_taken FROM state_history").fetchone()
        assert action is not None
        assert "rejected" in action[0].lower()

    def test_low_severity_no_notification_sent(self, ssh_ok, llm_low_fix, db_path):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_low_fix, notifier=notifier, gate=gate
            )
        )
        assert len(notifier.sent) == 0

    def test_low_severity_proceeds_directly_to_backup(
        self, ssh_ok, llm_low_fix, db_path
    ):
        from utils.notify import FakeNotifier
        from utils.autonomy import FakeAutonomyGate

        notifier = FakeNotifier(approve=True)
        gate = FakeAutonomyGate(auto_execute_result=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_low_fix, notifier=notifier, gate=gate
            )
        )
        assert any("ha backup new" in cmd for cmd in ssh_ok.commands_run)


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

    def test_hitl_timeout_minutes_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HITL_TIMEOUT_MINUTES == 60

    def test_hitl_timeout_minutes_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"hitl_timeout_minutes": 30}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.HITL_TIMEOUT_MINUTES == 30

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


# ── AutonomyGate ──────────────────────────────────────────────────────────────────


class TestAutonomyGate:
    # ── should_auto_execute: 4 levels × 4 risks ──────────────────────────────────

    def test_level1_never_auto_executes(self):
        from utils.autonomy import AutonomyGate, RiskLevel

        gate = AutonomyGate(level=1)
        for risk in RiskLevel:
            assert gate.should_auto_execute(risk) is False

    def test_level2_never_auto_executes(self):
        from utils.autonomy import AutonomyGate, RiskLevel

        gate = AutonomyGate(level=2)
        for risk in RiskLevel:
            assert gate.should_auto_execute(risk) is False

    def test_level3_auto_executes_low_only(self):
        from utils.autonomy import AutonomyGate, RiskLevel

        gate = AutonomyGate(level=3)
        assert gate.should_auto_execute(RiskLevel.LOW) is True
        assert gate.should_auto_execute(RiskLevel.MEDIUM) is False
        assert gate.should_auto_execute(RiskLevel.HIGH) is False
        assert gate.should_auto_execute(RiskLevel.CRITICAL) is False

    def test_level4_auto_executes_except_critical(self):
        from utils.autonomy import AutonomyGate, RiskLevel

        gate = AutonomyGate(level=4)
        assert gate.should_auto_execute(RiskLevel.LOW) is True
        assert gate.should_auto_execute(RiskLevel.MEDIUM) is True
        assert gate.should_auto_execute(RiskLevel.HIGH) is True
        assert gate.should_auto_execute(RiskLevel.CRITICAL) is False

    # ── should_ask_preference ────────────────────────────────────────────────────

    def test_levels_1_2_3_ask_preference(self):
        from utils.autonomy import AutonomyGate

        for level in [1, 2, 3]:
            assert AutonomyGate(level=level).should_ask_preference("context") is True

    def test_level4_does_not_ask_preference(self):
        from utils.autonomy import AutonomyGate

        assert AutonomyGate(level=4).should_ask_preference("context") is False

    # ── require_approval short-circuits ─────────────────────────────────────────

    def test_level1_rejects_without_notifying_for_all_risks(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=1)
        for risk in RiskLevel:
            notifier = FakeNotifier(approve=True)
            result = asyncio.run(
                gate.require_approval(
                    "s", "b", {"notification_id": "x"}, notifier, risk
                )
            )
            assert result is False
            assert len(notifier.sent) == 0

    def test_level4_approves_low_without_notifying(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.LOW
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_level4_approves_medium_without_notifying(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.MEDIUM
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_level4_approves_high_without_notifying(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.HIGH
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_level4_notifies_for_critical_and_approves(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=True)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "test-id"}, notifier, RiskLevel.CRITICAL
            )
        )
        assert result is True
        assert len(notifier.sent) == 1

    def test_level4_notifies_for_critical_and_rejects(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "test-id"}, notifier, RiskLevel.CRITICAL
            )
        )
        assert result is False
        assert len(notifier.sent) == 1

    def test_level3_approves_low_without_notifying(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=3)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.LOW
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_level3_notifies_for_medium(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=3)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.MEDIUM
            )
        )
        assert len(notifier.sent) == 1

    def test_level3_notifies_for_high(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=3)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.HIGH
            )
        )
        assert len(notifier.sent) == 1

    def test_level3_notifies_for_critical(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=3)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.CRITICAL
            )
        )
        assert len(notifier.sent) == 1

    def test_level2_notifies_for_all_risks(self):
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=2)
        for risk in RiskLevel:
            notifier = FakeNotifier(approve=True)
            asyncio.run(
                gate.require_approval(
                    "s", "b", {"notification_id": "x"}, notifier, risk
                )
            )
            assert len(notifier.sent) == 1, f"level 2 should notify for {risk.name}"

    def test_require_approval_timeout_returns_false(self, monkeypatch):
        import asyncio as asyncio_mod
        from utils.autonomy import AutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        async def immediate_timeout(coro, timeout):
            coro.close()
            raise asyncio_mod.TimeoutError()

        monkeypatch.setattr(asyncio_mod, "wait_for", immediate_timeout)
        gate = AutonomyGate(level=2, timeout_minutes=60)
        notifier = FakeNotifier(approve=True)
        result = asyncio.run(
            gate.require_approval(
                "s", "b", {"notification_id": "x"}, notifier, RiskLevel.HIGH
            )
        )
        assert result is False

    # ── done criteria — level 1: no SSH writes ───────────────────────────────────

    def test_level1_sandbox_pipeline_produces_no_ssh_writes(self):
        from utils.autonomy import AutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport
        import ha_agent_sandbox_engine

        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: s1\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )
        llm = FakeLLMClient(
            DiagnosticsReport(
                is_valid=False,
                severity="WARNING",
                identified_issues=["deprecated key"],
                recommended_fix_yaml=_FIXED_CONFIG,
            ).model_dump_json()
        )
        gate = AutonomyGate(level=1)
        notifier = FakeNotifier(approve=True)
        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )
        assert ssh.written_files == {}

    # ── done criteria — level 4: full pipeline for WARNING, pauses for CRITICAL ──

    def test_level4_warning_severity_runs_without_hitl(self):
        from utils.autonomy import AutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport
        import ha_agent_sandbox_engine

        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: s1\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )
        llm = FakeLLMClient(
            DiagnosticsReport(
                is_valid=False,
                severity="WARNING",
                identified_issues=["deprecated key"],
                recommended_fix_yaml=_FIXED_CONFIG,
            ).model_dump_json()
        )
        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=True)
        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )
        assert len(notifier.sent) == 0
        assert any("ha backup new" in cmd for cmd in ssh.commands_run)

    def test_level4_critical_severity_pauses_for_approval(self):
        from utils.autonomy import AutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from ha_agent_sandbox_engine import DiagnosticsReport
        import ha_agent_sandbox_engine

        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _ORIGINAL_CONFIG},
            command_results={
                "ha backup new": (0, "Slug: s1\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )
        llm = FakeLLMClient(
            DiagnosticsReport(
                is_valid=False,
                severity="CRITICAL",
                identified_issues=["critical error"],
                recommended_fix_yaml=_FIXED_CONFIG,
            ).model_dump_json()
        )
        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=True)
        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )
        assert len(notifier.sent) == 1


# ── netalertx.* config keys ─────────────────────────────────────────────────────


class TestNetAlertXConfigKeys:
    def test_netalertx_enabled_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_ENABLED is False

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

        assert "jokob-sk/NetAlertX" in config.NETALERTX_ADDON_REPOSITORY_URL

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
                        "enabled": True,
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

        assert config.NETALERTX_ENABLED is True
        assert config.NETALERTX_API_PORT == 9999
        assert config.NETALERTX_API_TOKEN == "tok123"
        assert config.NETALERTX_MAX_SCAN_AGE_MINUTES == 5
        assert config.NETALERTX_MQTT_SUBSCRIBE is False
        assert config.NETALERTX_MAX_DB_HISTORY_ROWS == 50000


# ── netalertx/detector.py ───────────────────────────────────────────────────────


class TestNetAlertXDetector:
    def _make_http_client(self, version: str):
        import httpx
        import json

        class _VersionTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                body = json.dumps({"success": True, "value": version}).encode()
                return httpx.Response(
                    200,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )

        return httpx.AsyncClient(transport=_VersionTransport())

    def test_supervisor_path_returns_addon_mode(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        ssh = FakeSSHClient(
            command_results={"ha supervisor info": (0, "supervisor: ok\n", "")}
        )
        http = self._make_http_client("v26.7.1")
        result = asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
        )
        assert result.mode == "addon"
        assert result.api_base_url == "http://ha.local:20212"
        assert result.version == "v26.7.1"
        assert result.log_path == "/data/app.log"
        assert result.container_name == "netalertx"

    def test_docker_fallback_when_supervisor_fails(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", "command not found"),
                "docker info": (0, "Server Version: 24.0\n", ""),
            }
        )
        http = self._make_http_client("v26.7.1")
        result = asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
        )
        assert result.mode == "docker"

    def test_raises_when_neither_supervisor_nor_docker(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", "not found"),
                "docker info": (1, "", "not found"),
            }
        )
        http = self._make_http_client("")
        with pytest.raises(RuntimeError, match="deployment detection failed"):
            asyncio.run(
                detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
            )

    def test_version_empty_when_api_unreachable(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment
        import httpx

        class _ErrorTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("refused")

        ssh = FakeSSHClient(command_results={"ha supervisor info": (0, "ok", "")})
        http = httpx.AsyncClient(transport=_ErrorTransport())
        result = asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
        )
        assert result.version == ""

    def test_supervisor_command_is_tried_first(self):
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "docker info": (0, "ok", ""),
            }
        )
        http = self._make_http_client("")
        asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=http)
        )
        assert "ha supervisor info" in ssh.commands_run
        # Docker should not be probed when supervisor succeeds
        assert "docker info" not in ssh.commands_run

    def test_fetch_version_without_injected_client(self, monkeypatch):
        import netalertx.detector as det_mod
        from utils.ssh_client import FakeSSHClient
        from netalertx.detector import detect_deployment

        version_client = self._make_http_client("v99.0.0")
        monkeypatch.setattr(
            det_mod.httpx, "AsyncClient", lambda **kwargs: version_client
        )

        ssh = FakeSSHClient(command_results={"ha supervisor info": (0, "ok", "")})
        result = asyncio.run(
            detect_deployment(ssh, "ha.local", 20212, "netalertx", http_client=None)
        )
        assert result.version == "v99.0.0"


# ── netalertx/api_client.py ─────────────────────────────────────────────────────


class TestNetAlertXAPIClient:
    def _mock_client(self, routes: list):
        """Build an httpx.AsyncClient backed by a simple in-memory transport.

        routes: list of (method, url_fragment, status_code, json_body)
        """
        import httpx
        import json

        class _MockTransport(httpx.AsyncBaseTransport):
            def __init__(self, routes):
                self._routes = routes

            async def handle_async_request(self, request):
                for method, fragment, status, body in self._routes:
                    if request.method == method and fragment in str(request.url):
                        if isinstance(body, str):
                            content = body.encode()
                            ct = "text/plain"
                        else:
                            content = json.dumps(body).encode()
                            ct = "application/json"
                        return httpx.Response(
                            status,
                            content=content,
                            headers={"Content-Type": ct},
                        )
                return httpx.Response(
                    404,
                    content=b'{"error":"not found"}',
                    headers={"Content-Type": "application/json"},
                )

        return httpx.AsyncClient(transport=_MockTransport(routes))

    def _client(self, routes):
        from netalertx.api_client import NetAlertXAPIClient

        return NetAlertXAPIClient(
            base_url="http://nax.local:20212",
            api_token="testtoken",
            http_client=self._mock_client(routes),
        )

    def test_get_devices_returns_list(self):
        devices = [
            {"devMAC": "AA:BB:CC:DD:EE:FF", "devName": "laptop"},
            {"devMAC": "11:22:33:44:55:66", "devName": "phone"},
        ]
        c = self._client(
            [("GET", "/devices", 200, {"success": True, "devices": devices})]
        )
        result = asyncio.run(c.get_devices())
        assert len(result) == 2
        assert result[0]["devMAC"] == "AA:BB:CC:DD:EE:FF"

    def test_get_events_returns_list(self):
        events = [
            {
                "eveMac": "AA:BB:CC:DD:EE:FF",
                "eveIp": "192.168.1.10",
                "eveEventType": "New Device",
            }
        ]
        c = self._client([("GET", "/events", 200, {"success": True, "events": events})])
        result = asyncio.run(c.get_events())
        assert len(result) == 1
        assert result[0]["eveEventType"] == "New Device"

    def test_get_metrics_parses_prometheus_text(self):
        prometheus_text = (
            "# HELP netalertx_connected_devices\n"
            "# TYPE netalertx_connected_devices gauge\n"
            "netalertx_connected_devices 31\n"
            "netalertx_offline_devices 54\n"
            "netalertx_down_devices 0\n"
            'netalertx_device_status{device="laptop",mac="AA:BB"} 1\n'
        )
        c = self._client([("GET", "/metrics", 200, prometheus_text)])
        result = asyncio.run(c.get_metrics())
        assert result["connected_devices"] == 31.0
        assert result["offline_devices"] == 54.0
        assert result["down_devices"] == 0.0
        assert "device_status" not in result  # labeled metric skipped

    def test_get_settings_via_graphql(self):
        gql_response = {
            "data": {
                "settings": {
                    "settings": [
                        {"setKey": "LOADED_PLUGINS", "setValue": "ARPSCAN,MQTT"},
                        {"setKey": "MQTT_BROKER", "setValue": "192.168.1.1"},
                    ],
                    "count": 2,
                }
            }
        }
        c = self._client([("POST", "/graphql", 200, gql_response)])
        result = asyncio.run(c.get_settings())
        assert result["LOADED_PLUGINS"] == "ARPSCAN,MQTT"
        assert result["MQTT_BROKER"] == "192.168.1.1"

    def test_trigger_scan_posts_to_correct_endpoint(self):
        c = self._client(
            [
                (
                    "POST",
                    "/nettools/trigger-scan",
                    200,
                    {"success": True, "message": "queued"},
                )
            ]
        )
        # Should not raise
        asyncio.run(c.trigger_scan())

    def test_trigger_scan_raises_on_error(self):
        c = self._client(
            [
                (
                    "POST",
                    "/nettools/trigger-scan",
                    500,
                    {"success": False, "error": "fail"},
                )
            ]
        )
        with pytest.raises(Exception):
            asyncio.run(c.trigger_scan())

    def test_get_about_returns_health_dict(self):
        health = {"success": True, "db_size_mb": 12.4, "mem_usage_pct": 45}
        c = self._client([("GET", "/health", 200, health)])
        result = asyncio.run(c.get_about())
        assert result["success"] is True
        assert result["db_size_mb"] == 12.4

    def test_get_devices_empty_list_when_no_devices(self):
        c = self._client([("GET", "/devices", 200, {"success": True, "devices": []})])
        result = asyncio.run(c.get_devices())
        assert result == []

    def test_get_without_injected_client_creates_own_session(self, monkeypatch):
        import httpx
        import netalertx.api_client as ac_mod
        from netalertx.api_client import NetAlertXAPIClient

        mock = self._mock_client(
            [("GET", "/devices", 200, {"success": True, "devices": []})]
        )
        monkeypatch.setattr(ac_mod.httpx, "AsyncClient", lambda **kwargs: mock)

        c = NetAlertXAPIClient(base_url="http://nax.local:20212", api_token="tok")
        result = asyncio.run(c.get_devices())
        assert result == []

    def test_post_without_injected_client_creates_own_session(self, monkeypatch):
        import netalertx.api_client as ac_mod
        from netalertx.api_client import NetAlertXAPIClient

        mock = self._mock_client(
            [
                (
                    "POST",
                    "/nettools/trigger-scan",
                    200,
                    {"success": True, "message": "ok"},
                )
            ]
        )
        monkeypatch.setattr(ac_mod.httpx, "AsyncClient", lambda **kwargs: mock)

        c = NetAlertXAPIClient(base_url="http://nax.local:20212", api_token="tok")
        asyncio.run(c.trigger_scan())

    def test_parse_prometheus_metrics_skips_invalid_float(self):
        prometheus_text = (
            "netalertx_connected_devices 31\n" "netalertx_bad_metric not_a_number\n"
        )
        c = self._client([("GET", "/metrics", 200, prometheus_text)])
        result = asyncio.run(c.get_metrics())
        assert result["connected_devices"] == 31.0
        assert "bad_metric" not in result


# ── netalertx SQLite migration ──────────────────────────────────────────────────


class TestNetAlertXMigration:
    def test_migration_creates_install_state_table(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        db = tmp_path / "test.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()

        with sqlite3.connect(db) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "netalertx_install_state" in tables

    def test_install_state_table_has_correct_columns(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        db = tmp_path / "test.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()

        with sqlite3.connect(db) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(netalertx_install_state)")
            }
        assert {"id", "state", "correlation_id", "timestamp", "details_json"} == cols

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        db = tmp_path / "test.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()  # second run must not raise

        with sqlite3.connect(db) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 3


# ── netalertx/installer.py (steps 1–4) ───────────────────────────────────────


def _make_installer_db(tmp_path, monkeypatch):
    """Create and migrate a test SQLite DB, patch DB_PATH in installer module."""
    import ha_agent_advanced
    import netalertx.installer as inst

    db = tmp_path / "installer_test.db"
    monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
    ha_agent_advanced.init_local_database()
    monkeypatch.setattr(inst, "DB_PATH", str(db))
    return str(db)


class TestNetAlertXInstallerSteps1to4:
    # ── helpers ──────────────────────────────────────────────────────────────

    def _make_supervisor_ssh(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "supervisor_info: ok", ""),
                "ha addons info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (
                    0,
                    "default via 192.168.1.1 dev eth0 proto dhcp",
                    "",
                ),
                "ha store repositories list": (
                    0,
                    "https://github.com/jokob-sk/NetAlertX",
                    "",
                ),
                "ha store addons": (
                    0,
                    "slug: jokob-sk_NetAlertX\nrepository: jokob-sk/NetAlertX",
                    "",
                ),
            }
        )

    def _make_docker_ssh(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", "not found"),
                "docker info": (0, "docker info ok", ""),
                "ha addons info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (
                    0,
                    "default via 10.0.0.1 dev wlan0 proto dhcp",
                    "",
                ),
                "ha store repositories list": (
                    0,
                    "https://github.com/jokob-sk/NetAlertX",
                    "",
                ),
                "ha store addons": (0, "", ""),
            }
        )

    def _notifier(self, approve: bool = True):
        from utils.notify import FakeNotifier

        return FakeNotifier(approve=approve)

    def _gate_auto(self):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=True)

    def _gate_ask(self):
        """Gate that always requests HITL approval (outcome determined by notifier)."""
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=False)

    # ── step 1: detect deployment ─────────────────────────────────────────────

    def test_step1_supervisor_sets_mode_addon(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        state = asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        assert state == "ADDON_REPO_ADDED"
        _, details = _read_install_state(db)
        assert details["mode"] == "addon"

    def test_step1_docker_fallback_requires_approval(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "wlan0")

        gate = self._gate_ask()
        state = asyncio.run(
            run_steps_1_to_4(
                self._make_docker_ssh(), gate, self._notifier(approve=True), db_path=db
            )
        )
        assert state == "ADDON_REPO_ADDED"
        _, details = _read_install_state(db)
        assert details["mode"] == "docker"
        assert any(
            c["subject"] == "NetAlertX installer: Docker fallback"
            for c in gate.require_approval_calls
        )

    def test_step1_docker_fallback_rejection_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        state = asyncio.run(
            run_steps_1_to_4(
                self._make_docker_ssh(),
                self._gate_ask(),
                self._notifier(approve=False),
                db_path=db,
            )
        )
        assert state == "NOT_INSTALLED"

    def test_step1_no_target_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", ""),
                "docker info": (1, "", ""),
            }
        )
        gate = self._gate_auto()
        state = asyncio.run(run_steps_1_to_4(ssh, gate, self._notifier(), db_path=db))
        assert state == "NOT_INSTALLED"
        # CRITICAL approval requested even though auto_execute_result=True
        # (level 4 still notifies on CRITICAL, but gate returns True — state won't advance
        #  because no mode was set and step returns False)
        assert any(
            "no deployment target" in c["subject"] for c in gate.require_approval_calls
        )

    # ── step 2: mosquitto ─────────────────────────────────────────────────────

    def test_step2_mosquitto_already_running_skips_install(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        gate = self._gate_auto()
        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(), gate, self._notifier(), db_path=db
            )
        )
        # No Mosquitto install approval should have been requested
        assert not any(
            "install Mosquitto" in c["subject"] for c in gate.require_approval_calls
        )

    def test_step2_mosquitto_not_installed_requires_approval(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        call_counts: dict[str, int] = {}

        class TrackingSSHClient(FakeSSHClient):
            async def run(self, command, check=False):  # type: ignore[override]
                call_counts[command] = call_counts.get(command, 0) + 1
                # After install+start, polling returns running
                if (
                    "ha addons info core_mosquitto" in command
                    and call_counts.get(command, 0) > 1
                ):
                    return 0, "state: running", ""
                return await super().run(command, check=check)

        ssh = TrackingSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons info core_mosquitto": (0, "not found", ""),
                "ha addons install core_mosquitto": (0, "", ""),
                "ha addons start core_mosquitto": (0, "", ""),
                "ip route show default": (0, "default via 1.1.1.1 dev eth0", ""),
                "ha store repositories list": (
                    0,
                    "https://github.com/jokob-sk/NetAlertX",
                    "",
                ),
                "ha store addons": (0, "slug: jokob-sk_NetAlertX", ""),
            }
        )
        gate = self._gate_ask()
        asyncio.run(
            run_steps_1_to_4(ssh, gate, self._notifier(approve=True), db_path=db)
        )
        assert any(
            "install Mosquitto" in c["subject"] for c in gate.require_approval_calls
        )

    def test_step2_mosquitto_install_rejection_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons info core_mosquitto": (0, "not found", ""),
            }
        )
        state = asyncio.run(
            run_steps_1_to_4(
                ssh, self._gate_ask(), self._notifier(approve=False), db_path=db
            )
        )
        # State advanced to MQTT_INSTALLED (step 1 done) but not MQTT_RUNNING
        assert state == "MQTT_INSTALLED"

    # ── step 3: interface detection ───────────────────────────────────────────

    def test_step3_single_interface_auto_detected(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "")

        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        _, details = _read_install_state(db)
        assert details["scan_interface"] == "eth0"

    def test_step3_config_interface_overrides_detection(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "br0")

        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        _, details = _read_install_state(db)
        assert details["scan_interface"] == "br0"

    def test_step3_multiple_interfaces_requires_approval(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "")

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (
                    0,
                    "default via 1.1.1.1 dev eth0\ndefault via 2.2.2.2 dev wlan0",
                    "",
                ),
                "ha store repositories list": (
                    0,
                    "https://github.com/jokob-sk/NetAlertX",
                    "",
                ),
                "ha store addons": (0, "slug: jokob-sk_NetAlertX", ""),
            }
        )
        gate = self._gate_ask()
        state = asyncio.run(
            run_steps_1_to_4(ssh, gate, self._notifier(approve=True), db_path=db)
        )
        assert state == "ADDON_REPO_ADDED"
        _, details = _read_install_state(db)
        assert details["scan_interface"] == "eth0"
        assert any(
            "confirm scan interface" in c["subject"]
            for c in gate.require_approval_calls
        )

    def test_step3_multiple_interfaces_rejection_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "")

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (
                    0,
                    "default via 1.1.1.1 dev eth0\ndefault via 2.2.2.2 dev wlan0",
                    "",
                ),
            }
        )
        state = asyncio.run(
            run_steps_1_to_4(
                ssh, self._gate_ask(), self._notifier(approve=False), db_path=db
            )
        )
        assert state == "MQTT_RUNNING"

    def test_step3_no_interface_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "")

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (0, "no route found", ""),
            }
        )
        state = asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )
        assert state == "MQTT_RUNNING"

    # ── step 4: add repo + slug ────────────────────────────────────────────────

    def test_step4_repo_already_present_skips_add(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        ssh = self._make_supervisor_ssh()
        asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )

        add_calls = [c for c in ssh.commands_run if "repositories add" in c]
        assert len(add_calls) == 0

    def test_step4_repo_add_aborts_if_verify_fails(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")
        monkeypatch.setattr(
            "netalertx.installer.NETALERTX_ADDON_REPOSITORY_URL",
            "https://github.com/jokob-sk/NetAlertX",
        )

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (0, "default via 1.1.1.1 dev eth0", ""),
                # List returns empty both times → add appears to fail
                "ha store repositories list": (0, "", ""),
                "ha store repositories add": (0, "", ""),
                "ha store addons": (0, "", ""),
            }
        )
        gate = self._gate_auto()
        state = asyncio.run(
            run_steps_1_to_4(ssh, gate, self._notifier(approve=True), db_path=db)
        )
        assert state == "MQTT_RUNNING"
        assert any(
            "repository add failed" in c["subject"] for c in gate.require_approval_calls
        )

    def test_step4_slug_resolved_from_store(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        _, details = _read_install_state(db)
        assert details.get("addon_slug") == "jokob-sk_NetAlertX"

    def test_step4_slug_from_config_takes_precedence(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")
        monkeypatch.setattr(
            "netalertx.installer.NETALERTX_ADDON_SLUG", "my_custom_slug"
        )

        asyncio.run(
            run_steps_1_to_4(
                self._make_supervisor_ssh(),
                self._gate_auto(),
                self._notifier(),
                db_path=db,
            )
        )
        _, details = _read_install_state(db)
        assert details["addon_slug"] == "my_custom_slug"

    # ── idempotency ────────────────────────────────────────────────────────────

    def test_second_run_is_noop_at_addon_repo_added(self, tmp_path, monkeypatch):
        import asyncio
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        ssh = self._make_supervisor_ssh()
        asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )

        # Second run with a fresh SSH client tracking commands
        ssh2 = self._make_supervisor_ssh()
        asyncio.run(
            run_steps_1_to_4(ssh2, self._gate_auto(), self._notifier(), db_path=db)
        )

        # On second run no commands should have been executed (all steps skipped)
        assert len(ssh2.commands_run) == 0

    def test_parse_slug_from_store_finds_slug(self):
        from netalertx.installer import _parse_slug_from_store

        output = (
            "- name: NetAlertX\n"
            "  slug: jokob-sk_NetAlertX\n"
            "  repository: https://github.com/jokob-sk/NetAlertX\n"
        )
        slug = _parse_slug_from_store(output, "https://github.com/jokob-sk/NetAlertX")
        assert slug == "jokob-sk_NetAlertX"

    def test_parse_slug_from_store_returns_empty_when_not_found(self):
        from netalertx.installer import _parse_slug_from_store

        slug = _parse_slug_from_store(
            "no relevant content here", "https://github.com/jokob-sk/NetAlertX"
        )
        assert slug == ""

    def test_step2_mosquitto_not_running_starts_without_installing(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        call_counts: dict[str, int] = {}

        class TrackingSSHClient(FakeSSHClient):
            async def run(self, command, check=False):  # type: ignore[override]
                call_counts[command] = call_counts.get(command, 0) + 1
                if "ha addons info core_mosquitto" in command:
                    if call_counts[command] == 1:
                        return 0, "state: stopped", ""
                    return 0, "state: running", ""
                return await super().run(command, check=check)

        ssh = TrackingSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons start core_mosquitto": (0, "", ""),
                "ip route show default": (0, "default via 1.1.1.1 dev eth0", ""),
                "ha store repositories list": (
                    0,
                    "https://github.com/jokob-sk/NetAlertX",
                    "",
                ),
                "ha store addons": (0, "slug: jokob-sk_NetAlertX", ""),
            }
        )
        asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )
        assert "ha addons start core_mosquitto" in ssh.commands_run
        assert "ha addons install core_mosquitto" not in ssh.commands_run

    def test_step2_mosquitto_start_poll_fails_aborts(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)

        async def poll_false(*a, **k):
            return False

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_false)

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons info core_mosquitto": (0, "state: stopped", ""),
                "ha addons start core_mosquitto": (0, "", ""),
            }
        )
        gate = self._gate_ask()
        state = asyncio.run(
            run_steps_1_to_4(ssh, gate, self._notifier(approve=False), db_path=db)
        )
        assert state == "MQTT_INSTALLED"
        assert any(
            "failed to start" in c["subject"].lower()
            for c in gate.require_approval_calls
        )

    def test_step3_interface_already_in_details_is_idempotent(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from netalertx.installer import run_steps_1_to_4, _read_install_state

        db = _make_installer_db(tmp_path, monkeypatch)
        from netalertx.installer import _write_install_state

        _write_install_state(
            str(db),
            "MQTT_RUNNING",
            {"scan_interface": "eth1"},
            "test-cid",
        )

        ssh = self._make_supervisor_ssh()
        asyncio.run(
            run_steps_1_to_4(ssh, self._gate_auto(), self._notifier(), db_path=db)
        )
        _, details = _read_install_state(str(db))
        assert details["scan_interface"] == "eth1"
        assert not any("interface" in c.lower() for c in ssh.commands_run)

    def test_step4_repo_freshly_added_advances_state(self, tmp_path, monkeypatch):
        import asyncio
        from utils.ssh_client import FakeSSHClient
        from netalertx.installer import run_steps_1_to_4

        db = _make_installer_db(tmp_path, monkeypatch)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")

        from netalertx.installer import _write_install_state

        _write_install_state(str(db), "MQTT_RUNNING", {}, "test-cid")

        _repo_url = "https://github.com/jokob-sk/NetAlertX"
        list_call_count = [0]

        class TrackingSSHClient(FakeSSHClient):
            async def run(self, command, check=False):  # type: ignore[override]
                if "ha store repositories list" in command:
                    list_call_count[0] += 1
                    if list_call_count[0] == 1:
                        return 0, "", ""
                    return 0, _repo_url, ""
                return await super().run(command, check=check)

        ssh = TrackingSSHClient(
            command_results={
                f"ha store repositories add {_repo_url}": (0, "", ""),
                "ha store addons": (0, "slug: jokob-sk_NetAlertX", ""),
            }
        )
        gate = self._gate_auto()
        state = asyncio.run(run_steps_1_to_4(ssh, gate, self._notifier(), db_path=db))
        assert state == "ADDON_REPO_ADDED"
        assert list_call_count[0] == 2


# ── installer DB helper (shared by Steps1to4 and Steps5to8 tests) ─────────────


def _make_installer_db_at_state(tmp_path, monkeypatch, state: str, details=None):
    """Create and migrate a test DB pre-seeded at *state*, patch DB_PATH."""
    import ha_agent_advanced
    import netalertx.installer as inst

    db = tmp_path / "installer_test.db"
    monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
    ha_agent_advanced.init_local_database()
    monkeypatch.setattr(inst, "DB_PATH", str(db))

    from netalertx.installer import _write_install_state

    _write_install_state(str(db), state, details or {}, "test-cid")
    return str(db)


# ── Steps 5–8 helper function unit tests ─────────────────────────────────────


class TestInstallerHelpers5to8:
    def test_merge_app_conf_updates_existing_key(self):
        from netalertx.installer import _merge_app_conf

        original = "MQTT_BROKER = 'old'\nMQTT_PORT = 1234\n"
        merged, diff = _merge_app_conf(original, {"MQTT_BROKER": "'new'"})
        assert "MQTT_BROKER = 'new'" in merged
        assert "MQTT_PORT = 1234" in merged
        assert "MQTT_BROKER" in diff

    def test_merge_app_conf_appends_missing_key(self):
        from netalertx.installer import _merge_app_conf

        original = "MQTT_PORT = 1883\n"
        merged, diff = _merge_app_conf(original, {"HA_URL": "'http://ha.local:8123'"})
        assert "HA_URL = 'http://ha.local:8123'" in merged
        assert "HA_URL" in diff

    def test_merge_app_conf_preserves_unchanged_key(self):
        from netalertx.installer import _merge_app_conf

        original = "MQTT_BROKER = 'same'\n"
        merged, diff = _merge_app_conf(original, {"MQTT_BROKER": "'same'"})
        assert "MQTT_BROKER = 'same'" in merged
        assert "MQTT_BROKER" not in diff  # no change → not in diff

    def test_merge_plugins_adds_missing_plugins(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("['ARPSCAN']", ["MQTT", "ARPSCAN"])
        import ast

        lst = ast.literal_eval(result)
        assert "MQTT" in lst
        assert "ARPSCAN" in lst

    def test_merge_plugins_preserves_existing_plugins(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("['ARPSCAN', 'NMAP', 'MQTT']", ["MQTT", "ARPSCAN"])
        import ast

        lst = ast.literal_eval(result)
        assert "NMAP" in lst
        assert lst.count("MQTT") == 1  # no duplicates

    def test_merge_plugins_handles_empty_original(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("", ["MQTT", "ARPSCAN"])
        import ast

        lst = ast.literal_eval(result)
        assert set(lst) == {"MQTT", "ARPSCAN"}

    def test_parse_data_path_returns_path(self):
        from netalertx.installer import _parse_data_path

        info = "name: NetAlertX\ndata: /data/netalertx\nstate: running\n"
        assert _parse_data_path(info) == "/data/netalertx"

    def test_parse_data_path_returns_empty_when_absent(self):
        from netalertx.installer import _parse_data_path

        assert _parse_data_path("name: NetAlertX\nstate: running\n") == ""

    def test_check_automation_exists_true(self):
        from netalertx.installer import _check_automation_exists

        content = "- id: netalertx_event_handler\n  trigger:\n    - platform: webhook\n"
        assert _check_automation_exists(content) is True

    def test_check_automation_exists_false(self):
        from netalertx.installer import _check_automation_exists

        content = "- id: other_automation\n  trigger:\n    - platform: state\n"
        assert _check_automation_exists(content) is False

    def test_merge_plugins_non_list_literal_resets(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("'hello'", ["MQTT"])
        import ast

        lst = ast.literal_eval(result)
        assert lst == ["MQTT"]

    def test_merge_plugins_invalid_literal_resets(self):
        from netalertx.installer import _merge_plugins

        result = _merge_plugins("{broken syntax!!", ["MQTT"])
        import ast

        lst = ast.literal_eval(result)
        assert lst == ["MQTT"]

    def test_poll_addon_state_timeout_returns_false(self):
        import asyncio
        from netalertx.installer import _poll_addon_state
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ha addons info slug_x": (0, "state: stopped", "")}
        )
        result = asyncio.run(
            _poll_addon_state(ssh, "slug_x", "running", attempts=2, delay=0.0)
        )
        assert result is False

    def test_poll_addon_not_state_timeout_returns_false(self):
        import asyncio
        from netalertx.installer import _poll_addon_not_state
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ha addons info slug_y": (0, "state: unknown", "")}
        )
        result = asyncio.run(
            _poll_addon_not_state(ssh, "slug_y", "unknown", attempts=2, delay=0.0)
        )
        assert result is False

    def test_poll_addon_not_state_returns_true_when_state_changes(self):
        import asyncio
        from netalertx.installer import _poll_addon_not_state
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ha addons info slug_z": (0, "state: running", "")}
        )
        result = asyncio.run(
            _poll_addon_not_state(ssh, "slug_z", "unknown", attempts=2, delay=0.0)
        )
        assert result is True

    def test_detect_subnet_returns_empty_when_no_inet(self):
        import asyncio
        from netalertx.installer import _detect_subnet
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ip addr show eth0": (0, "link/ether aa:bb:cc:dd:ee:ff\n", "")
            }
        )
        result = asyncio.run(_detect_subnet(ssh, "eth0"))
        assert result == ""

    def test_detect_subnet_returns_empty_on_invalid_ip(self):
        import asyncio
        from netalertx.installer import _detect_subnet
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={"ip addr show eth0": (0, "inet 999.999.999.999/99\n", "")}
        )
        result = asyncio.run(_detect_subnet(ssh, "eth0"))
        assert result == ""


# ── TestNetAlertXInstallerSteps5to8 ──────────────────────────────────────────

_SLUG = "jokob-sk_NetAlertX"
_DATA_PATH = "/data/netalertx"
_CONF_PATH = f"{_DATA_PATH}/app.conf"
_ORIG_APP_CONF = "MQTT_BROKER = 'localhost'\nLOADED_PLUGINS = ['ARPSCAN']\n"
_HA_CONF = "homeassistant:\n  time_zone: America/New_York\n"
_AUTOMATIONS_PATH = "/config/automations.yaml"


def _make_mock_http(routes):
    """Build an httpx.AsyncClient backed by an in-memory transport.

    routes: list of (method, url_fragment, status_code, response_body)
    """
    import json

    import httpx

    class _T(httpx.AsyncBaseTransport):
        def __init__(self, rs):
            self._rs = rs

        async def handle_async_request(self, request):
            for method, frag, status, body in self._rs:
                if request.method == method and frag in str(request.url):
                    if isinstance(body, str):
                        content, ct = body.encode(), "text/plain"
                    else:
                        content, ct = json.dumps(body).encode(), "application/json"
                    return httpx.Response(
                        status, content=content, headers={"Content-Type": ct}
                    )
            return httpx.Response(
                404,
                content=b"not found",
                headers={"Content-Type": "text/plain"},
            )

    return httpx.AsyncClient(transport=_T(routes))


class TestNetAlertXInstallerSteps5to8:
    # ── helpers ──────────────────────────────────────────────────────────────

    def _notifier(self, approve: bool = True):
        from utils.notify import FakeNotifier

        return FakeNotifier(approve=approve)

    def _gate_auto(self):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=True)

    def _gate_ask(self, approval: bool = True):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=False, approval_result=approval)

    def _make_full_ssh(self, app_conf=_ORIG_APP_CONF, automations=""):
        """SSH client configured for a typical steps-5-8 run (all pass)."""
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient(
            file_contents={
                _CONF_PATH: app_conf,
                "/config/configuration.yaml": _HA_CONF,
                _AUTOMATIONS_PATH: automations,
            },
            command_results={
                f"ha addons info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                f"ha addons install {_SLUG}": (0, "", ""),
                f"ha addons start {_SLUG}": (0, "", ""),
                f"ha addons restart {_SLUG}": (0, "", ""),
                "ha backup new": (0, "Slug: test-backup-slug\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                f"ip addr show": (
                    0,
                    "inet 192.168.1.5/24 brd 192.168.1.255 scope global eth0\n",
                    "",
                ),
            },
        )

    def _http_with_mqtt(self):
        return _make_mock_http(
            [
                ("GET", "/api/config/config_entries", 200, [{"domain": "mqtt"}]),
                ("GET", "/health", 200, {"status": "ok"}),
            ]
        )

    def _http_no_mqtt(self):
        return _make_mock_http(
            [
                ("GET", "/api/config/config_entries", 200, [{"domain": "other"}]),
                ("GET", "/health", 200, {"status": "ok"}),
            ]
        )

    # ── step 5: install addon ─────────────────────────────────────────────────

    def test_step5_fresh_install_happy_path(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import _read_install_state, run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_not_state", poll_true)
        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                _CONF_PATH: _ORIG_APP_CONF,
                "/config/configuration.yaml": _HA_CONF,
                _AUTOMATIONS_PATH: "",
            },
            command_results={
                f"ha addons info {_SLUG}": (
                    0,
                    f"state: unknown\ndata: {_DATA_PATH}\n",
                    "",
                ),
                f"ha addons install {_SLUG}": (0, "", ""),
                f"ha addons start {_SLUG}": (0, "", ""),
                f"ha addons restart {_SLUG}": (0, "", ""),
                "ha backup new": (0, "Slug: fresh-slug\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "ip addr show": (
                    0,
                    "inet 192.168.1.5/24 scope global eth0\n",
                    "",
                ),
            },
        )

        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert any("ha addons install" in c for c in ssh.commands_run)
        assert any("ha addons start" in c for c in ssh.commands_run)

    def test_step5_already_running_skips_install_and_start(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import _read_install_state, run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        # No install or start commands issued since addon reported running
        assert not any("ha addons install" in c for c in ssh.commands_run)
        assert not any("ha addons start" in c for c in ssh.commands_run)

    def test_step5_install_timeout_triggers_critical_gate(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_false(*a, **k):
            return False

        monkeypatch.setattr("netalertx.installer._poll_addon_not_state", poll_false)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                f"ha addons info {_SLUG}": (0, "state: unknown\n", ""),
                f"ha addons install {_SLUG}": (0, "", ""),
            }
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_REPO_ADDED"
        assert any(
            c.get("risk").name == "CRITICAL" for c in gate.require_approval_calls
        )

    def test_step5_start_timeout_triggers_critical_gate(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_not_state_true(*a, **k):
            return True

        async def poll_state_false(*a, **k):
            return False

        monkeypatch.setattr(
            "netalertx.installer._poll_addon_not_state", poll_not_state_true
        )
        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_state_false)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                f"ha addons info {_SLUG}": (0, "state: unknown\n", ""),
                f"ha addons install {_SLUG}": (0, "", ""),
                f"ha addons start {_SLUG}": (0, "", ""),
            }
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_INSTALLED"
        assert any(
            c.get("risk").name == "CRITICAL" for c in gate.require_approval_calls
        )

    def test_step5_no_slug_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        db = _make_installer_db_at_state(
            tmp_path, monkeypatch, "ADDON_REPO_ADDED", {}  # no addon_slug in details
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_REPO_ADDED"
        assert any(
            c.get("risk").name == "CRITICAL" for c in gate.require_approval_calls
        )

    def test_step5_idempotent_from_addon_running(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        # Step 5 is entirely skipped — no install or start commands
        assert not any("ha addons install" in c for c in ssh.commands_run)
        assert not any("ha addons start" in c for c in ssh.commands_run)

    # ── step 6: configure app.conf ────────────────────────────────────────────

    def test_step6_writes_app_conf_and_merges_plugins(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )

        written = ssh.written_files.get(_CONF_PATH, "")
        assert "MQTT" in written
        assert "ARPSCAN" in written
        assert "MQTT_BROKER" in written

    def test_step6_backup_failure_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                _CONF_PATH: _ORIG_APP_CONF,
                "/config/configuration.yaml": _HA_CONF,
            },
            command_results={
                f"ha addons info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                "ha backup new": (1, "", "backup failed"),  # backup fails
                "ip addr show": (0, "inet 10.0.0.1/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_RUNNING"
        assert _CONF_PATH not in ssh.written_files

    def test_step6_idempotent_from_configured(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        # Step 6 was skipped — no app.conf write
        assert _CONF_PATH not in ssh.written_files

    # ── step 7: verify MQTT integration ──────────────────────────────────────

    def test_step7_mqtt_found_advances(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import (
            _read_install_state,
            run_steps_5_to_8,
        )  # noqa: F401

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"

    def test_step7_mqtt_not_found_approval_advances(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        # Gate asks and approves — MQTT still not visible in API but user confirmed
        gate = self._gate_ask(approval=True)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=True),
                db_path=db,
                http_client=self._http_no_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert gate.require_approval_calls  # HITL notification was sent

    def test_step7_mqtt_not_found_rejection_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=False),
                db_path=db,
                http_client=self._http_no_mqtt(),
            )
        )
        assert state == "NETALERTX_CONFIGURED"

    def test_step7_idempotent_from_mqtt_verified(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        gate = self._gate_ask(approval=False)  # no gate calls expected
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert not gate.require_approval_calls  # step 7 was skipped

    # ── step 8: create webhook automation ────────────────────────────────────

    def test_step8_creates_automation_and_reaches_fully_operational(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = self._make_full_ssh(automations="")  # empty automations file
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        written = ssh.written_files.get(_AUTOMATIONS_PATH, "")
        assert "netalertx_event_handler" in written
        assert "platform: webhook" in written
        assert "eveMac" in written  # camelCase field

    def test_step8_automation_exists_skips_write(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        existing = (
            "- id: netalertx_event_handler\n"
            "  trigger:\n    - platform: webhook\n      webhook_id: netalertx_event\n"
        )
        ssh = self._make_full_ssh(automations=existing)
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert _AUTOMATIONS_PATH not in ssh.written_files  # no write — already present

    def test_step8_ha_check_fails_restores_original(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        original_automations = "- id: existing_automation\n  trigger: []\n"

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                _AUTOMATIONS_PATH: original_automations,
                "/config/configuration.yaml": _HA_CONF,
                _CONF_PATH: _ORIG_APP_CONF,
            },
            command_results={
                "ha backup new": (0, "Slug: step8-slug\n", ""),
                "ha core check": (1, "", "automation error"),  # check fails
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "HA_MQTT_INTEGRATION_VERIFIED"
        # Original was restored
        assert ssh.written_files.get(_AUTOMATIONS_PATH) == original_automations
        assert any(c.get("risk").name == "HIGH" for c in gate.require_approval_calls)

    def test_step8_idempotent_from_fully_operational(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "FULLY_OPERATIONAL",
            {"addon_slug": _SLUG},
        )

        ssh2 = self._make_full_ssh()
        asyncio.run(
            run_steps_5_to_8(
                ssh2,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert len(ssh2.commands_run) == 0  # complete no-op

    # ── full run tests ────────────────────────────────────────────────────────

    def test_full_run_from_addon_repo_added_reaches_fully_operational(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer._poll_addon_not_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                _CONF_PATH: _ORIG_APP_CONF,
                "/config/configuration.yaml": _HA_CONF,
                _AUTOMATIONS_PATH: "",
            },
            command_results={
                f"ha addons info {_SLUG}": (
                    0,
                    f"state: unknown\ndata: {_DATA_PATH}\n",
                    "",
                ),
                f"ha addons install {_SLUG}": (0, "", ""),
                f"ha addons start {_SLUG}": (0, "", ""),
                f"ha addons restart {_SLUG}": (0, "", ""),
                "ha backup new": (0, "Slug: full-run-slug\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.2/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_REPO_ADDED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )

        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"

    def test_full_run_second_call_is_noop(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "FULLY_OPERATIONAL",
            {"addon_slug": _SLUG},
        )

        ssh2 = self._make_full_ssh()
        state = asyncio.run(
            run_steps_5_to_8(
                ssh2,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert len(ssh2.commands_run) == 0

    # ── step 6: error paths ───────────────────────────────────────────────────

    def test_step6_no_data_path_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = FakeSSHClient(
            file_contents={"/config/configuration.yaml": _HA_CONF},
            command_results={
                f"ha addons info {_SLUG}": (0, "state: running\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path, monkeypatch, "ADDON_RUNNING", {"addon_slug": _SLUG}
        )
        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=False),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_RUNNING"
        assert any("app.conf" in c["subject"] for c in gate.require_approval_calls)

    def test_step6_app_conf_missing_uses_empty_original(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": _HA_CONF,
                _AUTOMATIONS_PATH: "",
            },
            command_results={
                f"ha addons info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                "ha backup new": (0, "Slug: bk-slug\n", ""),
                f"ha addons restart {_SLUG}": (0, "", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.2/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert _CONF_PATH in ssh.written_files

    def test_step6_no_scan_interface_uses_empty_subnets(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        written = ssh.written_files.get(_CONF_PATH, "")
        assert "SCAN_SUBNETS = []" in written

    def test_step6_ha_conf_read_fails_uses_utc_timezone(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = FakeSSHClient(
            file_contents={_CONF_PATH: _ORIG_APP_CONF, _AUTOMATIONS_PATH: ""},
            command_results={
                f"ha addons info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                "ha backup new": (0, "Slug: bk-tz\n", ""),
                f"ha addons restart {_SLUG}": (0, "", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.1/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        written = ssh.written_files.get(_CONF_PATH, "")
        assert "TIMEZONE = 'UTC'" in written

    def test_step6_restart_poll_fails_triggers_gate(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        call_counts: dict[str, int] = {}

        async def poll_once_false(ssh_client, addon_id, expected, **kwargs):
            key = f"{addon_id}:{expected}"
            call_counts[key] = call_counts.get(key, 0) + 1
            if expected == "running" and call_counts[key] == 1:
                return False
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_once_false)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        ssh = FakeSSHClient(
            file_contents={
                _CONF_PATH: _ORIG_APP_CONF,
                "/config/configuration.yaml": _HA_CONF,
            },
            command_results={
                f"ha addons info {_SLUG}": (
                    0,
                    f"state: running\ndata: {_DATA_PATH}\n",
                    "",
                ),
                "ha backup new": (0, "Slug: bk-restart\n", ""),
                f"ha addons restart {_SLUG}": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.1/24 scope global eth0\n", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=False),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "ADDON_RUNNING"
        assert any(
            "restart" in c["subject"].lower() for c in gate.require_approval_calls
        )

    def test_step6_api_health_check_non_200_is_non_fatal(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        class _Non200HealthTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "/health" in str(request.url):
                    return httpx.Response(
                        503, content=b"", headers={"Content-Type": "text/plain"}
                    )
                return httpx.Response(
                    200,
                    content=b'[{"domain":"mqtt"}]',
                    headers={"Content-Type": "application/json"},
                )

        http = httpx.AsyncClient(transport=_Non200HealthTransport())
        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"

    def test_step6_api_health_check_exception_is_non_fatal(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        class _ErrorTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "/health" in str(request.url):
                    raise httpx.ConnectError("refused")
                return httpx.Response(
                    200,
                    content=b'[{"domain":"mqtt"}]',
                    headers={"Content-Type": "application/json"},
                )

        http = httpx.AsyncClient(transport=_ErrorTransport())
        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "ADDON_RUNNING",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"

    # ── step 7: _mqtt_configured edge paths ──────────────────────────────────

    def test_step7_mqtt_check_non_200_triggers_hitl(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        class _Non200Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(
                    503, content=b"", headers={"Content-Type": "text/plain"}
                )

        http = httpx.AsyncClient(transport=_Non200Transport())
        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        gate = self._gate_ask(approval=True)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=True),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert gate.require_approval_calls

    def test_step7_mqtt_check_exception_triggers_hitl(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        class _ExceptionTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("refused")

        http = httpx.AsyncClient(transport=_ExceptionTransport())
        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        gate = self._gate_ask(approval=True)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=True),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert gate.require_approval_calls

    def test_step7_approved_and_recheck_returns_true(self, tmp_path, monkeypatch):
        import asyncio

        import httpx

        from netalertx.installer import run_steps_5_to_8

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        call_count = [0]

        class _MqttAppearsAfterApproval(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if "/api/config/config_entries" in str(request.url):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        body = b'[{"domain":"other"}]'
                    else:
                        body = b'[{"domain":"mqtt"}]'
                    return httpx.Response(
                        200, content=body, headers={"Content-Type": "application/json"}
                    )
                return httpx.Response(
                    200, content=b"{}", headers={"Content-Type": "application/json"}
                )

        http = httpx.AsyncClient(transport=_MqttAppearsAfterApproval())
        ssh = self._make_full_ssh()
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "NETALERTX_CONFIGURED",
            {"addon_slug": _SLUG, "scan_interface": "eth0"},
        )
        gate = self._gate_ask(approval=True)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=True),
                db_path=db,
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert call_count[0] == 2

    # ── step 8: error paths ───────────────────────────────────────────────────

    def test_step8_backup_fails_aborts(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = FakeSSHClient(
            file_contents={_AUTOMATIONS_PATH: ""},
            command_results={
                "ha backup new": (1, "", "backup error"),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG},
        )
        gate = self._gate_ask(approval=False)
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                gate,
                self._notifier(approve=False),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "HA_MQTT_INTEGRATION_VERIFIED"
        assert any(
            "backup" in c["subject"].lower() for c in gate.require_approval_calls
        )

    def test_step8_fallback_to_directory_automation_path(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        _fallback_path = "/config/automations/netalertx_webhook.yaml"
        ssh = FakeSSHClient(
            file_contents={_fallback_path: ""},
            command_results={
                "ha backup new": (0, "Slug: bk-fallback\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert _fallback_path in ssh.written_files

    def test_step8_both_automation_paths_missing_creates_new_file(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        from netalertx.installer import run_steps_5_to_8
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)

        ssh = FakeSSHClient(
            file_contents={},
            command_results={
                "ha backup new": (0, "Slug: bk-new\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
            },
        )
        db = _make_installer_db_at_state(
            tmp_path,
            monkeypatch,
            "HA_MQTT_INTEGRATION_VERIFIED",
            {"addon_slug": _SLUG},
        )
        state = asyncio.run(
            run_steps_5_to_8(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=db,
                http_client=self._http_with_mqtt(),
            )
        )
        assert state == "FULLY_OPERATIONAL"
        assert "/config/automations/netalertx_webhook.yaml" in ssh.written_files


# ── run_installer ─────────────────────────────────────────────────────────────


class TestRunInstaller:
    def _gate_auto(self):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=True)

    def _notifier(self):
        from utils.notify import FakeNotifier

        return FakeNotifier(approve=True)

    def test_run_installer_chains_steps_1_to_4_then_5_to_8(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_installer
        from utils.ssh_client import FakeSSHClient

        async def poll_true(*a, **k):
            return True

        monkeypatch.setattr("netalertx.installer._poll_addon_state", poll_true)
        monkeypatch.setattr("netalertx.installer._poll_addon_not_state", poll_true)
        monkeypatch.setattr("netalertx.installer.NETALERTX_SCAN_INTERFACE", "eth0")
        monkeypatch.setattr("netalertx.installer.NETALERTX_ADDON_SLUG", "")

        _slug = "jokob-sk_NetAlertX"
        _data = "/data/netalertx"
        _conf = f"{_data}/app.conf"

        import ha_agent_advanced
        import netalertx.installer as inst

        db = tmp_path / "installer_full.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()
        monkeypatch.setattr(inst, "DB_PATH", str(db))

        import httpx

        class _FullTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                body = b'[{"domain":"mqtt"}]'
                return httpx.Response(
                    200, content=body, headers={"Content-Type": "application/json"}
                )

        http = httpx.AsyncClient(transport=_FullTransport())

        ssh = FakeSSHClient(
            file_contents={
                _conf: "MQTT_BROKER = 'localhost'\n",
                "/config/configuration.yaml": "homeassistant:\n  time_zone: UTC\n",
                "/config/automations.yaml": "",
            },
            command_results={
                "ha supervisor info": (0, "ok", ""),
                "ha addons info core_mosquitto": (0, "state: running", ""),
                "ip route show default": (0, "default via 1.1.1.1 dev eth0", ""),
                "ha store repositories list": (
                    0,
                    "https://github.com/jokob-sk/NetAlertX",
                    "",
                ),
                "ha store addons": (0, f"slug: {_slug}", ""),
                f"ha addons info {_slug}": (0, f"state: running\ndata: {_data}\n", ""),
                f"ha addons install {_slug}": (0, "", ""),
                f"ha addons start {_slug}": (0, "", ""),
                f"ha addons restart {_slug}": (0, "", ""),
                "ha backup new": (0, "Slug: full-slug\n", ""),
                "ha core check": (0, "", ""),
                "ha core reload": (0, "", ""),
                "ip addr show": (0, "inet 10.0.0.1/24 scope global eth0\n", ""),
            },
        )
        state = asyncio.run(
            run_installer(
                ssh,
                self._gate_auto(),
                self._notifier(),
                db_path=str(db),
                http_client=http,
            )
        )
        assert state == "FULLY_OPERATIONAL"

    def test_run_installer_aborts_when_steps_1_to_4_fail(self, tmp_path, monkeypatch):
        import asyncio

        from netalertx.installer import run_installer
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate

        import ha_agent_advanced
        import netalertx.installer as inst

        db = tmp_path / "installer_abort.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()
        monkeypatch.setattr(inst, "DB_PATH", str(db))

        ssh = FakeSSHClient(
            command_results={
                "ha supervisor info": (1, "", "not found"),
                "docker info": (1, "", "not found"),
            }
        )
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        state = asyncio.run(
            run_installer(
                ssh,
                gate,
                self._notifier(),
                db_path=str(db),
            )
        )
        assert state == "NOT_INSTALLED"


# ── netalertx/ha_name_sync.py ────────────────────────────────────────────────


class _FakeNAXClient:
    """Minimal NetAlertXAPIClient double that records write calls."""

    def __init__(self, devices: list[dict]) -> None:
        self._devices = devices
        self.updates: list[tuple[str, str, str]] = []  # (mac, col, val)
        self.locks: list[tuple[str, str, bool]] = []  # (mac, field, lock)

    async def get_devices(self) -> list[dict]:
        return self._devices

    async def update_device_column(
        self, mac: str, column_name: str, column_value: str
    ) -> None:
        self.updates.append((mac, column_name, column_value))

    async def lock_device_field(
        self, mac: str, field_name: str, lock: bool = True
    ) -> None:
        self.locks.append((mac, field_name, lock))


def _ha_states_transport(states: list[dict]):
    """Return an httpx.AsyncClient whose /api/states response is ``states``."""
    import json as _json

    class _T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=_json.dumps(states).encode())

    return httpx.AsyncClient(transport=_T())


def _make_syncer(
    ssh,
    nax_client,
    ha_http_client,
    patterns=None,
    gate=None,
    notifier=None,
):
    from netalertx.ha_name_sync import HaNameSync
    from utils.autonomy import FakeAutonomyGate
    from utils.notify import FakeNotifier

    return HaNameSync(
        ssh_client=ssh,
        api_client=nax_client,
        gate=gate or FakeAutonomyGate(auto_execute_result=True),
        notifier=notifier or FakeNotifier(approve=True),
        ha_host="ha.local",
        ha_api_token="test-tok",
        auto_patterns=patterns or ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"],
        http_client=ha_http_client,
    )


class TestHaNameSyncReadSources:
    def test_source3_only_returns_device_tracker_names(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        states = [
            {
                "entity_id": "device_tracker.phone",
                "attributes": {
                    "mac_address": "aa:bb:cc:dd:ee:ff",
                    "friendly_name": "Andy's Phone",
                },
            }
        ]
        ssh = FakeSSHClient()  # no files → Source 1 and 2 raise/skip
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names["AA:BB:CC:DD:EE:FF"] == "Andy's Phone"

    def test_source2_fills_gaps_not_covered_by_source3(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        # Source 3 has phone; Source 2 has a tablet not in Source 3
        states = [
            {
                "entity_id": "device_tracker.phone",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:01",
                    "friendly_name": "Phone",
                },
            }
        ]
        known_devices_yaml = "tablet:\n" "  mac: AA:BB:CC:DD:EE:02\n" "  name: Tablet\n"
        ssh = FakeSSHClient(
            file_contents={"/config/known_devices.yaml": known_devices_yaml}
        )
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names["AA:BB:CC:DD:EE:01"] == "Phone"
        assert names["AA:BB:CC:DD:EE:02"] == "Tablet"

    def test_source1_wins_over_source3_for_same_mac(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:FF"
        states = [
            {
                "entity_id": "device_tracker.x",
                "attributes": {"mac_address": mac, "friendly_name": "Old Name"},
            }
        ]
        registry_json = {
            "data": {
                "devices": [
                    {
                        "name": "Authoritative Name",
                        "name_by_user": None,
                        "connections": [["mac", mac]],
                    }
                ]
            }
        }
        import json as _json

        ssh = FakeSSHClient(
            file_contents={
                "/config/.storage/core.device_registry": _json.dumps(registry_json)
            }
        )
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names[mac] == "Authoritative Name"

    def test_source1_prefers_name_by_user_over_name(self):
        import asyncio
        import json as _json

        from utils.ssh_client import FakeSSHClient

        mac = "11:22:33:44:55:66"
        registry_json = {
            "data": {
                "devices": [
                    {
                        "name": "Auto Name",
                        "name_by_user": "Custom Name",
                        "connections": [["mac", mac]],
                    }
                ]
            }
        }
        ssh = FakeSSHClient(
            file_contents={
                "/config/.storage/core.device_registry": _json.dumps(registry_json)
            }
        )
        ha_http = _ha_states_transport([])
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names[mac] == "Custom Name"

    def test_source2_filenotfounderror_silently_skipped(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        states = [
            {
                "entity_id": "device_tracker.x",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:01",
                    "friendly_name": "X",
                },
            }
        ]
        # No known_devices.yaml → FakeSSHClient raises FileNotFoundError
        ssh = FakeSSHClient()
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert "AA:BB:CC:DD:EE:01" in names  # Source 3 still works

    def test_mac_normalization_various_formats(self):
        import asyncio
        import json as _json

        from utils.ssh_client import FakeSSHClient

        registry_json = {
            "data": {
                "devices": [
                    {
                        "name": "Dash Device",
                        "name_by_user": None,
                        "connections": [["mac", "aa-bb-cc-dd-ee-ff"]],
                    },
                ]
            }
        }
        states = [
            {
                "entity_id": "device_tracker.nocolon",
                "attributes": {
                    "mac_address": "aabbccddeeff",
                    "friendly_name": "No Colon",
                },
            }
        ]
        ssh = FakeSSHClient(
            file_contents={
                "/config/.storage/core.device_registry": _json.dumps(registry_json)
            }
        )
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        # dash-delimited MAC in Source 1 and no-delimiter MAC in Source 3
        # both normalize to the same key → Source 1 wins
        assert names.get("AA:BB:CC:DD:EE:FF") == "Dash Device"

    def test_source1_failure_falls_back_to_lower_sources(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        states = [
            {
                "entity_id": "device_tracker.phone",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:01",
                    "friendly_name": "Phone",
                },
            }
        ]
        # Bad JSON → Source 1 logs warning and skips
        ssh = FakeSSHClient(
            file_contents={"/config/.storage/core.device_registry": "not json {{{{"}
        )
        ha_http = _ha_states_transport(states)
        syncer = _make_syncer(ssh, _FakeNAXClient([]), ha_http)
        names = asyncio.run(syncer.read_ha_names())
        assert names["AA:BB:CC:DD:EE:01"] == "Phone"


class TestHaNameSyncCases1And2:
    _PATTERNS = ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"]

    def _simple_states(self, mac: str, name: str) -> list[dict]:
        return [
            {
                "entity_id": "device_tracker.x",
                "attributes": {"mac_address": mac, "friendly_name": name},
            }
        ]

    def test_case1_blank_devname_writes_and_locks(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:01"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": ""}]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Living Room TV"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written
        assert ("AA:BB:CC:DD:EE:01", "devName", "Living Room TV") in nax.updates
        assert ("AA:BB:CC:DD:EE:01", "devName", True) in nax.locks
        assert report.locked == []
        assert report.conflicted == []
        assert report.unnamed == []

    def test_case1_mac_as_devname_triggers_write(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:02"
        # devName is the MAC address itself → matches auto-generated pattern
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": mac,
                    "devVendor": "Acme",
                    "devLastIP": "10.0.0.2",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Office Printer"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written
        assert len(nax.updates) == 1
        assert nax.updates[0][2] == "Office Printer"

    def test_case1_unknown_prefix_triggers_write(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:03"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "unknown-abc123",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Thermostat"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written

    def test_case2_matching_name_locks_only(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:04"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Kitchen Hub",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Kitchen Hub"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.locked
        assert report.written == []
        assert len(nax.updates) == 0
        assert len(nax.locks) == 1

    def test_case2_case_insensitive_match_locks_only(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:05"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "kitchen hub",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Kitchen Hub"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.locked
        assert report.written == []

    def test_case3_conflict_collected_no_write_on_rejection(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:06"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Bob's Laptop",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Alice's Laptop"))
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(),
            nax,
            ha_http,
            patterns=self._PATTERNS,
            gate=gate,
            notifier=FakeNotifier(approve=False),
        )
        report = asyncio.run(syncer.sync_names())

        assert len(report.conflicted) == 1
        assert report.conflicted[0].mac == mac
        assert report.conflicted[0].ha_name == "Alice's Laptop"
        assert report.conflicted[0].netalertx_name == "Bob's Laptop"
        assert report.written == []
        assert report.locked == []
        assert len(nax.updates) == 0
        assert len(nax.locks) == 0

    def test_case4_no_ha_name_and_auto_devname_collected_as_unnamed(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:07"
        # devName is a MAC address (auto-generated) → Step A does not apply;
        # FakeSSHClient returns empty stdout → Step B (DNS) fails → Step C (unnamed)
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": mac,  # MAC-as-name matches auto-generated pattern
                    "devVendor": "Synology",
                    "devLastIP": "10.0.0.100",
                }
            ]
        )
        ha_http = _ha_states_transport([])
        gate = FakeAutonomyGate(auto_execute_result=True)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        report = asyncio.run(syncer.sync_names())

        assert len(report.unnamed) == 1
        assert report.unnamed[0].mac == mac
        assert report.unnamed[0].vendor == "Synology"
        assert report.unnamed[0].last_ip == "10.0.0.100"
        assert report.written == []
        assert len(nax.updates) == 0
        assert len(nax.locks) == 0

    def test_zero_ha_names_triggers_low_hitl(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        nax = _FakeNAXClient([])
        ha_http = _ha_states_transport([])  # empty → zero MAC entries
        gate = FakeAutonomyGate(auto_execute_result=True)
        notifier = FakeNotifier(approve=True)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, gate=gate, notifier=notifier
        )
        asyncio.run(syncer.sync_names())

        assert len(gate.require_approval_calls) == 1
        from utils.autonomy import RiskLevel

        assert gate.require_approval_calls[0]["risk"] == RiskLevel.LOW

    def test_sync_report_json_round_trip(self):
        from netalertx.ha_name_sync import ConflictEntry, SyncReport, UnnamedEntry

        report = SyncReport(
            written=["AA:BB:CC:DD:EE:01"],
            locked=["AA:BB:CC:DD:EE:02"],
            conflicted=[
                ConflictEntry(mac="AA:BB:CC:DD:EE:03", ha_name="X", netalertx_name="Y")
            ],
            unnamed=[
                UnnamedEntry(mac="AA:BB:CC:DD:EE:04", vendor="Acme", last_ip="10.0.0.4")
            ],
        )
        restored = SyncReport.model_validate_json(report.model_dump_json())
        assert restored.written == ["AA:BB:CC:DD:EE:01"]
        assert restored.conflicted[0].ha_name == "X"
        assert restored.unnamed[0].vendor == "Acme"

    def test_multiple_devices_mixed_cases(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        devices = [
            {
                "devMAC": "AA:BB:CC:DD:EE:01",
                "devName": "",
                "devVendor": "",
                "devLastIP": "",
            },
            {
                "devMAC": "AA:BB:CC:DD:EE:02",
                "devName": "Hub",
                "devVendor": "",
                "devLastIP": "",
            },
            {
                "devMAC": "AA:BB:CC:DD:EE:03",
                "devName": "Old",
                "devVendor": "",
                "devLastIP": "",
            },
            {
                "devMAC": "AA:BB:CC:DD:EE:04",
                "devName": "",  # blank → no HA name + no DNS → Step C (unnamed)
                "devVendor": "X",
                "devLastIP": "",
            },
        ]
        states = [
            {
                "entity_id": "device_tracker.a",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:01",
                    "friendly_name": "Phone",
                },
            },
            {
                "entity_id": "device_tracker.b",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:02",
                    "friendly_name": "Hub",
                },
            },
            {
                "entity_id": "device_tracker.c",
                "attributes": {
                    "mac_address": "AA:BB:CC:DD:EE:03",
                    "friendly_name": "New",
                },
            },
            # No entry for 04 → Case 4 (blank devName + no DNS → unnamed)
        ]
        nax = _FakeNAXClient(devices)
        ha_http = _ha_states_transport(states)
        # Use rejection gate so Case 3 conflict is not auto-resolved
        from utils.autonomy import FakeAutonomyGate

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        report = asyncio.run(syncer.sync_names())

        assert "AA:BB:CC:DD:EE:01" in report.written  # Case 1: blank
        assert "AA:BB:CC:DD:EE:02" in report.locked  # Case 2: match
        assert any(c.mac == "AA:BB:CC:DD:EE:03" for c in report.conflicted)  # Case 3
        assert any(u.mac == "AA:BB:CC:DD:EE:04" for u in report.unnamed)  # Case 4


class TestHaNameSyncCases3And4:
    """Item 14 — conflict resolution (Case 3) and unknown-device fallbacks (Case 4)."""

    _PATTERNS = ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"]

    def _simple_states(self, mac: str, name: str) -> list[dict]:
        return [
            {
                "entity_id": "device_tracker.x",
                "attributes": {"mac_address": mac, "friendly_name": name},
            }
        ]

    # ── Case 3: name conflict ─────────────────────────────────────────────────

    def test_case3_approval_writes_and_locks_all(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:10"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Bob's Laptop",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Alice's Laptop"))
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        syncer = _make_syncer(
            FakeSSHClient(),
            nax,
            ha_http,
            patterns=self._PATTERNS,
            gate=gate,
            notifier=FakeNotifier(approve=True),
        )
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written
        assert ("AA:BB:CC:DD:EE:10", "devName", "Alice's Laptop") in nax.updates
        assert ("AA:BB:CC:DD:EE:10", "devName", True) in nax.locks
        assert len(report.conflicted) == 1  # still recorded
        assert len(gate.require_approval_calls) == 1
        from utils.autonomy import RiskLevel

        assert gate.require_approval_calls[0]["risk"] == RiskLevel.MEDIUM

    def test_case3_rejection_skips_all(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:11"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Bob's Laptop",
                    "devVendor": "",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Alice's Laptop"))
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(),
            nax,
            ha_http,
            patterns=self._PATTERNS,
            gate=gate,
            notifier=FakeNotifier(approve=False),
        )
        report = asyncio.run(syncer.sync_names())

        assert report.written == []
        assert len(nax.updates) == 0
        assert len(nax.locks) == 0
        assert len(report.conflicted) == 1

    def test_case3_multiple_conflicts_single_hitl_call(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        macs = ["AA:BB:CC:DD:EE:12", "AA:BB:CC:DD:EE:13"]
        devices = [
            {"devMAC": m, "devName": "Old Name", "devVendor": "", "devLastIP": ""}
            for m in macs
        ]
        states = [
            {
                "entity_id": f"device_tracker.d{i}",
                "attributes": {"mac_address": m, "friendly_name": "New Name"},
            }
            for i, m in enumerate(macs)
        ]
        nax = _FakeNAXClient(devices)
        ha_http = _ha_states_transport(states)
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        asyncio.run(syncer.sync_names())

        # Two conflicts → only one require_approval call (batched)
        assert len(gate.require_approval_calls) == 1

    # ── Case 4 Step A: plausible existing name ────────────────────────────────

    def test_case4_step_a_plausible_name_locked(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:20"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "NAS",  # non-empty, not auto-generated → Step A
                    "devVendor": "Synology",
                    "devLastIP": "10.0.0.50",
                }
            ]
        )
        ha_http = _ha_states_transport([])  # no HA name for this MAC
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.locked
        assert report.written == []
        assert report.unnamed == []
        assert len(nax.updates) == 0
        assert ("AA:BB:CC:DD:EE:20", "devName", True) in nax.locks

    # ── Case 4 Step B: reverse DNS ───────────────────────────────────────────

    def test_case4_step_b_reverse_dns_written(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:21"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "",
                    "devVendor": "HP",
                    "devLastIP": "10.0.0.5",
                }
            ]
        )
        ha_http = _ha_states_transport([])
        ssh = FakeSSHClient(
            command_results={
                "host 10.0.0.5": (
                    0,
                    "5.0.0.10.in-addr.arpa domain name pointer myprinter.local.",
                    "",
                )
            }
        )
        syncer = _make_syncer(ssh, nax, ha_http, patterns=self._PATTERNS)
        report = asyncio.run(syncer.sync_names())

        assert mac in report.written
        assert mac in report.reverse_dns
        assert ("AA:BB:CC:DD:EE:21", "devName", "myprinter.local") in nax.updates
        assert report.unnamed == []

    def test_case4_step_b_in_addr_arpa_hostname_skipped(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:22"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": "10.0.0.6"}]
        )
        ha_http = _ha_states_transport([])
        # DNS returns the PTR record itself — unusable
        ssh = FakeSSHClient(
            command_results={
                "host 10.0.0.6": (
                    0,
                    "6.0.0.10.in-addr.arpa domain name pointer 6.0.0.10.in-addr.arpa.",
                    "",
                )
            }
        )
        gate = FakeAutonomyGate(auto_execute_result=True)
        syncer = _make_syncer(ssh, nax, ha_http, patterns=self._PATTERNS, gate=gate)
        report = asyncio.run(syncer.sync_names())

        assert report.written == []
        assert len(report.unnamed) == 1
        assert report.unnamed[0].mac == mac

    def test_case4_step_b_no_dns_response_falls_to_step_c(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:23"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "",
                    "devVendor": "Acme",
                    "devLastIP": "10.0.0.7",
                }
            ]
        )
        ha_http = _ha_states_transport([])
        # FakeSSHClient returns empty stdout for unknown commands
        gate = FakeAutonomyGate(auto_execute_result=True)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        report = asyncio.run(syncer.sync_names())

        assert report.written == []
        assert len(report.unnamed) == 1
        assert report.unnamed[0].vendor == "Acme"

    # ── Case 4 Step C: unnamed HITL ──────────────────────────────────────────

    def test_case4_step_c_unnamed_hitl_fires_with_low_risk(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:24"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": ""}]
        )
        # Provide one HA name so the zero-MAC gate does not fire; the device's
        # MAC is absent so it still goes through Step C (unnamed).
        other_state = [
            {
                "entity_id": "device_tracker.other",
                "attributes": {
                    "mac_address": "11:22:33:44:55:66",
                    "friendly_name": "Other Device",
                },
            }
        ]
        ha_http = _ha_states_transport(other_state)
        gate = FakeAutonomyGate(auto_execute_result=True)
        syncer = _make_syncer(
            FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS, gate=gate
        )
        asyncio.run(syncer.sync_names())

        # unnamed HITL fires exactly once with LOW risk
        unnamed_calls = [
            c
            for c in gate.require_approval_calls
            if c["risk"] == RiskLevel.LOW and "Unnamed" in c["subject"]
        ]
        assert len(unnamed_calls) == 1

    # ── sync_device ───────────────────────────────────────────────────────────

    def test_sync_device_case1_writes_ha_name(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:30"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "", "devVendor": "", "devLastIP": ""}]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "Front Door Camera"))
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        asyncio.run(syncer.sync_device(mac))

        assert ("AA:BB:CC:DD:EE:30", "devName", "Front Door Camera") in nax.updates
        assert ("AA:BB:CC:DD:EE:30", "devName", True) in nax.locks

    def test_sync_device_conflict_triggers_medium_hitl(self):
        import asyncio

        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:31"
        nax = _FakeNAXClient(
            [{"devMAC": mac, "devName": "Old Name", "devVendor": "", "devLastIP": ""}]
        )
        ha_http = _ha_states_transport(self._simple_states(mac, "New HA Name"))
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        syncer = _make_syncer(
            FakeSSHClient(),
            nax,
            ha_http,
            patterns=self._PATTERNS,
            gate=gate,
            notifier=FakeNotifier(approve=False),
        )
        asyncio.run(syncer.sync_device(mac))

        conflict_calls = [
            c for c in gate.require_approval_calls if c["risk"] == RiskLevel.MEDIUM
        ]
        assert len(conflict_calls) == 1
        assert len(nax.updates) == 0

    def test_sync_device_not_found_does_not_crash(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        nax = _FakeNAXClient([])  # no devices at all
        ha_http = _ha_states_transport([])
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        asyncio.run(syncer.sync_device("AA:BB:CC:DD:EE:99"))  # should log and return

        assert len(nax.updates) == 0
        assert len(nax.locks) == 0

    def test_sync_device_step_a_locks_plausible_name(self):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        mac = "AA:BB:CC:DD:EE:32"
        nax = _FakeNAXClient(
            [
                {
                    "devMAC": mac,
                    "devName": "Printer",
                    "devVendor": "Canon",
                    "devLastIP": "",
                }
            ]
        )
        ha_http = _ha_states_transport([])  # no HA name
        syncer = _make_syncer(FakeSSHClient(), nax, ha_http, patterns=self._PATTERNS)
        asyncio.run(syncer.sync_device(mac))

        assert ("AA:BB:CC:DD:EE:32", "devName", True) in nax.locks
        assert len(nax.updates) == 0


# ── netalertx/log_monitor.py ──────────────────────────────────────────────────


class TestNetAlertXLogMonitor:
    # ── CRITICAL_LOG_PATTERN ──────────────────────────────────────────────────

    def test_pattern_matches_scan_error(self):
        from netalertx.log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search(
            "ERROR: ArpScan failed — network unreachable"
        )

    def test_pattern_matches_mqtt_error(self):
        from netalertx.log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search("ERROR MQTT broker connection refused")

    def test_pattern_matches_plugin_exception(self):
        from netalertx.log_monitor import CRITICAL_LOG_PATTERN

        assert CRITICAL_LOG_PATTERN.search("Exception in plugin ARPSCAN execution")

    def test_pattern_no_match_on_info_line(self):
        from netalertx.log_monitor import CRITICAL_LOG_PATTERN

        assert not CRITICAL_LOG_PATTERN.search("INFO scan completed successfully")

    # ── LogEvaluation schema ──────────────────────────────────────────────────

    def test_log_evaluation_valid_construction(self):
        from netalertx.log_monitor import LogEvaluation

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed",
            confidence_score=0.9,
        )
        assert ev.is_actionable is True
        assert ev.confidence_score == 0.9

    def test_log_evaluation_missing_field_raises(self):
        from pydantic import ValidationError

        from netalertx.log_monitor import LogEvaluation

        with pytest.raises(ValidationError):
            LogEvaluation(is_actionable=True)  # type: ignore[call-arg]

    def test_log_evaluation_json_round_trip(self):
        from netalertx.log_monitor import LogEvaluation

        ev = LogEvaluation(
            is_actionable=False,
            root_cause_summary="Transient noise",
            confidence_score=0.2,
        )
        assert LogEvaluation.model_validate_json(ev.model_dump_json()) == ev

    # ── analyze_log_line_with_ai ──────────────────────────────────────────────

    @pytest.fixture
    def llm_actionable(self):
        from utils.ollama_client import FakeLLMClient

        from netalertx.log_monitor import LogEvaluation

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed: network unreachable",
            confidence_score=0.95,
        )
        return FakeLLMClient(ev.model_dump_json())

    @pytest.fixture
    def llm_not_actionable(self):
        from utils.ollama_client import FakeLLMClient

        from netalertx.log_monitor import LogEvaluation

        ev = LogEvaluation(
            is_actionable=False,
            root_cause_summary="Transient warning",
            confidence_score=0.1,
        )
        return FakeLLMClient(ev.model_dump_json())

    def test_analyze_returns_log_evaluation(self, llm_actionable):
        import asyncio

        from netalertx.log_monitor import analyze_log_line_with_ai

        result = asyncio.run(
            analyze_log_line_with_ai(["ERROR ArpScan failed"], llm_actionable)
        )
        assert result.is_actionable is True
        assert result.confidence_score == 0.95

    def test_analyze_calls_llm_once(self, llm_actionable):
        import asyncio

        from netalertx.log_monitor import analyze_log_line_with_ai

        asyncio.run(analyze_log_line_with_ai(["ERROR scan error"], llm_actionable))
        assert len(llm_actionable.calls) == 1

    def test_analyze_returns_safe_default_on_llm_error(self):
        import asyncio

        from utils.ollama_client import FakeLLMClient

        from netalertx.log_monitor import analyze_log_line_with_ai

        # Malformed JSON triggers the except branch
        broken_llm = FakeLLMClient("{not valid json}")
        result = asyncio.run(analyze_log_line_with_ai(["ERROR ..."], broken_llm))
        assert result.is_actionable is False
        assert result.confidence_score == 0.0

    # ── stream behaviour ──────────────────────────────────────────────────────

    def test_stream_non_critical_lines_skip_triage(self, llm_not_actionable):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        from netalertx.log_monitor import tail_netalertx_log_stream

        ssh = FakeSSHClient(stream_data=["INFO scan completed", "DEBUG heartbeat"])
        asyncio.run(
            tail_netalertx_log_stream(ssh_client=ssh, llm_client=llm_not_actionable)
        )
        assert len(llm_not_actionable.calls) == 0

    def test_stream_critical_line_invokes_triage(self, llm_not_actionable, monkeypatch):
        import asyncio

        from utils.ssh_client import FakeSSHClient

        import netalertx.log_monitor as mod
        from netalertx.log_monitor import tail_netalertx_log_stream

        monkeypatch.setattr(mod._debouncer, "record", lambda: False)
        ssh = FakeSSHClient(
            stream_data=["ERROR ArpScan failed: network unreachable", "INFO ok"]
        )
        asyncio.run(
            tail_netalertx_log_stream(ssh_client=ssh, llm_client=llm_not_actionable)
        )
        assert len(llm_not_actionable.calls) == 1

    # ── autonomy gate: level 1 sends notifier, no healer ─────────────────────

    def test_level_1_sends_notifier_no_healer(self, monkeypatch):
        import asyncio

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient
        from utils.ssh_client import FakeSSHClient

        from netalertx.log_monitor import LogEvaluation

        import netalertx.log_monitor as mod

        healer_calls: list = []

        async def fake_healer(ev):
            healer_calls.append(ev)

        monkeypatch.setattr(mod, "_dispatch_to_healer", fake_healer)
        monkeypatch.setattr(mod._debouncer, "record", lambda: True)

        ev = LogEvaluation(
            is_actionable=True,
            root_cause_summary="ArpScan failed",
            confidence_score=0.95,
        )
        llm = FakeLLMClient(ev.model_dump_json())
        gate = FakeAutonomyGate(auto_execute_result=False)
        notifier = FakeNotifier()
        ssh = FakeSSHClient(stream_data=["ERROR ArpScan failed: network unreachable"])

        asyncio.run(
            mod.tail_netalertx_log_stream(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )

        assert len(notifier.sent) == 1
        assert healer_calls == []

    # ── reconnect on stream failure ───────────────────────────────────────────

    def test_stream_reconnects_on_ose_error(self, monkeypatch):
        import asyncio as asyncio_mod

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        import netalertx.log_monitor as mod

        async def no_sleep(_: float) -> None:
            pass

        monkeypatch.setattr(asyncio_mod, "sleep", no_sleep)

        call_count = [0]

        class FailOnceFakeSSH:
            async def read_file(self, path: str) -> str:
                raise FileNotFoundError(path)

            async def write_file(self, path: str, content: str) -> None:
                pass

            async def run(self, command: str, check: bool = False):
                return 0, "", ""

            async def stream_lines(self, command: str):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise OSError("connection lost")
                return
                yield  # makes this an async generator

        ssh = FailOnceFakeSSH()
        llm = FakeLLMClient("{}")
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        asyncio_mod.run(
            mod.tail_netalertx_log_stream(
                ssh_client=ssh, llm_client=llm, gate=gate, notifier=notifier
            )
        )

        assert call_count[0] == 2
