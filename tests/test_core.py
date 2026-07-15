#!/usr/bin/env python3
"""Pueo test suite — covers logic exercisable without external services."""

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


class TestMain:
    def test_missing_config_exits_1(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "main.py", "--config", str(tmp_path / "nonexistent.yaml")],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_invalid_mode_exits_2(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text("")
        result = subprocess.run(
            [sys.executable, "main.py", "--config", str(config), "--mode", "badmode"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert result.returncode == 2

    def test_help_exits_0(self):
        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert result.returncode == 0
        assert "monitor" in result.stdout
