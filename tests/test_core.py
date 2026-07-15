#!/usr/bin/env python3
"""Pueo test suite — covers logic exercisable without external services."""

import importlib
import sys

import pytest
import yaml
from pydantic import ValidationError


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
        isolated_config.write_text(yaml.dump({
            "home_assistant": {"host": "myha.local", "user": "admin"},
            "ollama": {"model": "llama3:8b"},
            "agent": {"log_confidence_threshold": 0.9, "db_path": "test.db"},
        }))
        importlib.reload(sys.modules["config"])
        import config
        assert config.HA_HOST == "myha.local"
        assert config.HA_USER == "admin"
        assert config.OLLAMA_MODEL == "llama3:8b"
        assert config.CONFIDENCE_THRESHOLD == 0.9
        assert config.DB_PATH == "test.db"

    def test_partial_config_falls_back_to_defaults(self, isolated_config):
        isolated_config.write_text(yaml.dump({
            "home_assistant": {"host": "partial.local"}
        }))
        importlib.reload(sys.modules["config"])
        import config
        assert config.HA_HOST == "partial.local"
        assert config.HA_USER == "root"           # default
        assert config.OLLAMA_MODEL == "qwen2.5-coder:7b"  # default

    def test_ssh_key_path_expands_tilde(self, isolated_config):
        isolated_config.write_text(yaml.dump({
            "home_assistant": {"ssh_key_path": "~/.ssh/id_rsa"}
        }))
        importlib.reload(sys.modules["config"])
        import config
        assert not config.SSH_KEY_PATH.startswith("~")
        assert "id_rsa" in config.SSH_KEY_PATH


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
            identified_issues=["Malformed YAML in sensor block", "Missing required key: platform"],
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
        isolated_config.write_text(yaml.dump({
            "home_assistant": {"config_path": "/custom/path/config.yaml"}
        }))
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
        assert CRITICAL_LOG_PATTERN.search(
            "ERROR Traceback (most recent call last):"
        )

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
        isolated_config.write_text(yaml.dump({
            "agent": {"log_confidence_threshold": 0.85}
        }))
        importlib.reload(sys.modules["config"])
        import ha_log_monitor
        importlib.reload(ha_log_monitor)
        assert ha_log_monitor.CONFIDENCE_THRESHOLD == 0.85
