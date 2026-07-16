#!/usr/bin/env python3
"""Pueo test suite — covers logic exercisable without external services."""

import asyncio
import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path

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

    def test_schema_version_is_2_after_init(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 2

    def test_version_unchanged_on_second_init(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 2

    def test_pre_migration_database_upgraded(self, db_path):
        import ha_agent_advanced

        # Simulate a database that existed before migration versioning was added
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE state_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER, config_hash TEXT,
                    is_valid INTEGER, issues_found TEXT, action_taken TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE backup_registry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER, backup_slug TEXT, status TEXT
                )
            """)
            conn.commit()

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 2

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
            conn.execute("""
                CREATE TABLE state_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER, config_hash TEXT,
                    is_valid INTEGER, issues_found TEXT, action_taken TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE backup_registry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER, backup_slug TEXT, status TEXT
                )
            """)
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

    def test_repair_path_writes_config(self, ssh_ok, llm_with_fix, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(ssh_client=ssh_ok, llm_client=llm_with_fix)
        )
        assert "/config/configuration.yaml" in ssh_ok.written_files
        assert ssh_ok.written_files["/config/configuration.yaml"] == _FIXED_CONFIG

    def test_repair_path_backup_recorded(self, ssh_ok, llm_with_fix, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(ssh_client=ssh_ok, llm_client=llm_with_fix)
        )
        with sqlite3.connect(db_path) as conn:
            slug = conn.execute("SELECT backup_slug FROM backup_registry").fetchone()
        assert slug is not None
        assert slug[0] == "sbx-slug-1"

    def test_sandbox_fail_aborts_atomic_swap(
        self, ssh_sandbox_fail, llm_with_fix, db_path
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_sandbox_fail, llm_client=llm_with_fix
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

    def test_sandbox_fail_records_state(self, ssh_sandbox_fail, llm_with_fix, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_sandbox_fail, llm_client=llm_with_fix
            )
        )
        with sqlite3.connect(db_path) as conn:
            action = conn.execute("SELECT action_taken FROM state_history").fetchone()
        assert action is not None
        assert "aborted" in action[0].lower()

    def test_repair_path_llm_called_once(self, ssh_ok, llm_with_fix, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(ssh_client=ssh_ok, llm_client=llm_with_fix)
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

        notifier = FakeNotifier(approve=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier
            )
        )
        assert len(notifier.sent) == 1

    def test_critical_issue_notification_contains_severity(
        self, ssh_ok, llm_critical, db_path
    ):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier(approve=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier
            )
        )
        assert "CRITICAL" in notifier.sent[0]["subject"]

    def test_approval_proceeds_to_backup(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier(approve=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier
            )
        )
        assert any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_rejection_aborts_backup(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier(approve=False)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier
            )
        )
        assert not any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_rejection_records_state(self, ssh_ok, llm_critical, db_path):
        from utils.notify import FakeNotifier
        import sqlite3 as sqlite3_mod

        notifier = FakeNotifier(approve=False)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_critical, notifier=notifier
            )
        )
        with sqlite3_mod.connect(db_path) as conn:
            action = conn.execute("SELECT action_taken FROM state_history").fetchone()
        assert action is not None
        assert "rejected" in action[0].lower()

    def test_low_severity_no_notification_sent(self, ssh_ok, llm_low_fix, db_path):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier(approve=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_low_fix, notifier=notifier
            )
        )
        assert len(notifier.sent) == 0

    def test_low_severity_proceeds_directly_to_backup(
        self, ssh_ok, llm_low_fix, db_path
    ):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier(approve=True)
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_low_fix, notifier=notifier
            )
        )
        assert any("ha backup new" in cmd for cmd in ssh_ok.commands_run)
