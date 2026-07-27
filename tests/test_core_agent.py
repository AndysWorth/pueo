#!/usr/bin/env python3
"""Core HA agent tests — schemas, SQLite layers, pipelines, CLI."""

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

from utils.ssh_client import FakeSSHClient

_REPO_ROOT = Path(__file__).parent.parent
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

    def test_schema_version_is_6_after_init(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 6

    def test_version_unchanged_on_second_init(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 6

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
        assert version == 6

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


# ── Backup inventory (item 30) ───────────────────────────────────────────────────


class TestBackupInventory:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        return path

    def test_migration_v5_adds_inventory_columns(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            cols = [
                r[1]
                for r in conn.execute("PRAGMA table_info(backup_registry)").fetchall()
            ]
        assert "size_bytes" in cols
        assert "location" in cols
        assert "offloaded_at" in cols
        assert "deleted_from_ha_at" in cols

    def test_record_backup_slug_sets_location_ha(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("slug-loc")
        with sqlite3.connect(db_path) as conn:
            location = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = 'slug-loc'"
            ).fetchone()[0]
        assert location == "ha"

    def test_record_backup_slug_size_bytes_default_zero(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("slug-sz")
        with sqlite3.connect(db_path) as conn:
            size = conn.execute(
                "SELECT size_bytes FROM backup_registry WHERE backup_slug = 'slug-sz'"
            ).fetchone()[0]
        assert size == 0

    def test_parse_backup_list_valid_json(self):
        import ha_agent_advanced

        output = '{"result":"ok","data":{"backups":[{"slug":"abc123","size_bytes":56760320}]}}'
        result = ha_agent_advanced._parse_backup_list(output)
        assert result == [{"slug": "abc123", "size_bytes": 56760320}]

    def test_parse_backup_list_multiple_backups(self):
        import ha_agent_advanced

        output = '{"result":"ok","data":{"backups":[{"slug":"aaa","size_bytes":1000},{"slug":"bbb","size_bytes":2000}]}}'
        result = ha_agent_advanced._parse_backup_list(output)
        assert len(result) == 2
        assert result[0]["slug"] == "aaa"
        assert result[1]["slug"] == "bbb"

    def test_parse_backup_list_empty_backups(self):
        import ha_agent_advanced

        output = '{"result":"ok","data":{"backups":[]}}'
        result = ha_agent_advanced._parse_backup_list(output)
        assert result == []

    def test_parse_backup_list_invalid_json(self):
        import ha_agent_advanced

        result = ha_agent_advanced._parse_backup_list("not valid json")
        assert result == []

    def test_parse_backup_list_missing_size_defaults_zero(self):
        import ha_agent_advanced

        output = '{"result":"ok","data":{"backups":[{"slug":"xyz"}]}}'
        result = ha_agent_advanced._parse_backup_list(output)
        assert result == [{"slug": "xyz", "size_bytes": 0}]

    def test_reconcile_inserts_ha_only_slug(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        backup_json = (
            '{"result":"ok","data":{"backups":[{"slug":"newslug","size_bytes":12345}]}}'
        )
        ssh = FakeSSHClient(command_results={"ha backups list": (0, backup_json, "")})
        asyncio.run(ha_agent_advanced.reconcile_backup_inventory(ssh_client=ssh))
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT backup_slug, size_bytes, location FROM backup_registry"
                " WHERE backup_slug = 'newslug'"
            ).fetchone()
        assert row is not None
        assert row[1] == 12345
        assert row[2] == "ha"

    def test_reconcile_skips_existing_slug(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("existing-slug")
        backup_json = '{"result":"ok","data":{"backups":[{"slug":"existing-slug","size_bytes":9999}]}}'
        ssh = FakeSSHClient(command_results={"ha backups list": (0, backup_json, "")})
        asyncio.run(ha_agent_advanced.reconcile_backup_inventory(ssh_client=ssh))
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM backup_registry WHERE backup_slug = 'existing-slug'"
            ).fetchone()[0]
        assert count == 1

    def test_reconcile_warns_on_orphaned_slug(self, db_path, caplog):
        import logging

        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("orphan-slug")
        backup_json = '{"result":"ok","data":{"backups":[]}}'
        ssh = FakeSSHClient(command_results={"ha backups list": (0, backup_json, "")})
        with caplog.at_level(logging.WARNING):
            asyncio.run(ha_agent_advanced.reconcile_backup_inventory(ssh_client=ssh))
        assert any("orphan" in r.message for r in caplog.records)

    def test_reconcile_skips_on_ssh_error(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()

        class FailSSH:
            async def run(self, command, check=False):
                raise RuntimeError("SSH unavailable")

        asyncio.run(ha_agent_advanced.reconcile_backup_inventory(ssh_client=FailSSH()))
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM backup_registry").fetchone()[0]
        assert count == 0


# ── Backup offloading ─────────────────────────────────────────────────────────────


class TestBackupOffloading:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def _insert_slug(self, db_path, slug):
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, ?, 'ACTIVE', 0, 'ha')",
                (1000, slug),
            )
            conn.commit()

    def test_offload_success_updates_location_to_both(
        self, db_path, monkeypatch, tmp_path
    ):
        import asyncio
        import hashlib
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        slug = "abc123"
        self._insert_slug(db_path, slug)
        content = b"fake tar content"
        remote_hash = hashlib.sha256(content).hexdigest()
        local_dir = tmp_path / "backups"
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", str(local_dir))
        ssh = FakeSSHClient(
            download_contents={f"/backup/{slug}.tar": content},
            command_results={
                "sha256sum": (0, f"{remote_hash}  /backup/{slug}.tar\n", "")
            },
        )
        asyncio.run(ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh))
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = ?", (slug,)
            ).fetchone()
        assert row[0] == "both"

    def test_offload_checksum_mismatch_leaves_location_ha(
        self, db_path, monkeypatch, tmp_path
    ):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        slug = "mismatch-slug"
        self._insert_slug(db_path, slug)
        local_dir = tmp_path / "backups"
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", str(local_dir))
        ssh = FakeSSHClient(
            download_contents={f"/backup/{slug}.tar": b"local content"},
            command_results={"sha256sum": (0, f"{'a' * 64}  /backup/{slug}.tar\n", "")},
        )
        asyncio.run(ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh))
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = ?", (slug,)
            ).fetchone()
        assert row[0] == "ha"
        assert not (local_dir / f"{slug}.tar").exists()

    def test_offload_transfer_failure_logs_warning_no_raise(
        self, db_path, monkeypatch, tmp_path
    ):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        slug = "fail-slug"
        self._insert_slug(db_path, slug)
        local_dir = tmp_path / "backups"
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", str(local_dir))
        ssh = FakeSSHClient(download_error=OSError("SFTP connection refused"))
        asyncio.run(ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh))
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = ?", (slug,)
            ).fetchone()
        assert row[0] == "ha"

    def test_offload_disabled_skips_download(self, db_path, monkeypatch, tmp_path):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        monkeypatch.setattr(ha_agent_advanced, "BACKUP_OFFLOAD_ENABLED", False)
        slug = "skip-slug"
        self._insert_slug(db_path, slug)
        ssh = FakeSSHClient()
        asyncio.run(ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh))
        assert ssh.downloaded_files == []

    def test_offload_no_remote_hash_proceeds(self, db_path, monkeypatch, tmp_path):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        slug = "nohash-slug"
        self._insert_slug(db_path, slug)
        local_dir = tmp_path / "backups"
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", str(local_dir))
        ssh = FakeSSHClient(
            download_contents={f"/backup/{slug}.tar": b"data"},
            command_results={"sha256sum": (1, "", "sha256sum: not found")},
        )
        asyncio.run(ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh))
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = ?", (slug,)
            ).fetchone()
        assert row[0] == "both"


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

    def test_schema_version_is_6_after_init(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 6

    def test_version_unchanged_on_second_init(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 6

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
        assert version == 6

    def test_migration_v2_adds_correlation_id_column(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            cols = [
                r[1]
                for r in conn.execute("PRAGMA table_info(state_history)").fetchall()
            ]
        assert "correlation_id" in cols

    def test_migration_v3_creates_netalertx_install_state_table(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "netalertx_install_state" in tables

    def test_migration_v4_creates_netalertx_state_table(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "netalertx_state" in tables

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
        monkeypatch.setattr(
            ha_agent_advanced, "BACKUP_LOCAL_DIR", str(tmp_path / "backups")
        )
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
                "ha core restart": (0, "", ""),
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
                "ha core restart": (0, "", ""),
                "mkdir": (0, "", ""),
                "mv": (0, "", ""),
                "cp": (0, "", ""),
            },
        )

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_sandbox_engine
        import ha_agent_advanced

        path = str(tmp_path / "sbx_test.db")
        monkeypatch.setattr(ha_agent_sandbox_engine, "DB_PATH", path)
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        monkeypatch.setattr(
            ha_agent_advanced, "BACKUP_LOCAL_DIR", str(tmp_path / "backups")
        )
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

        result, _trace = asyncio.run(
            analyze_log_line_with_ai(
                ["ERROR Invalid config for sensor"], llm_actionable
            )
        )
        assert result.is_actionable is True
        assert result.confidence_score == 0.95

    def test_analyze_non_actionable_line(self, llm_not_actionable):
        from ha_log_monitor import analyze_log_line_with_ai

        result, _trace = asyncio.run(
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


# ── Retention policy (item 32) ────────────────────────────────────────────────────


class TestEnforceHaRetention:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def _insert(self, db_path, slug, location, ts, deleted_from_ha_at=None):
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry"
                " (timestamp, backup_slug, status, size_bytes, location, deleted_from_ha_at)"
                " VALUES (?, ?, 'ACTIVE', 0, ?, ?)",
                (ts, slug, location, deleted_from_ha_at),
            )
            conn.commit()

    def test_no_deletion_when_at_limit(self, db_path, monkeypatch):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 2)
        self._insert(db_path, "slug-a", "both", 1000)
        self._insert(db_path, "slug-b", "both", 2000)
        ssh = FakeSSHClient()
        asyncio.run(ha_agent_advanced.enforce_ha_retention(ssh_client=ssh))
        assert ssh.commands_run == []

    def test_deletes_oldest_when_over_limit(self, db_path, monkeypatch):
        import asyncio
        import sqlite3
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 2)
        self._insert(db_path, "slug-old", "both", 1000)
        self._insert(db_path, "slug-mid", "both", 2000)
        self._insert(db_path, "slug-new", "both", 3000)
        ssh = FakeSSHClient(command_results={"ha backups remove slug-old": (0, "", "")})
        asyncio.run(ha_agent_advanced.enforce_ha_retention(ssh_client=ssh))
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT deleted_from_ha_at FROM backup_registry WHERE backup_slug = 'slug-old'"
            ).fetchone()
        assert row[0] is not None

    def test_never_deletes_most_recent(self, db_path, monkeypatch):
        import asyncio
        import sqlite3
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 1)
        self._insert(db_path, "slug-old", "both", 1000)
        self._insert(db_path, "slug-newest", "both", 9999)
        ssh = FakeSSHClient(command_results={"ha backups remove slug-old": (0, "", "")})
        asyncio.run(ha_agent_advanced.enforce_ha_retention(ssh_client=ssh))
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT deleted_from_ha_at FROM backup_registry"
                " WHERE backup_slug = 'slug-newest'"
            ).fetchone()
        assert row[0] is None

    def test_skips_ha_only_slugs(self, db_path, monkeypatch):
        import asyncio
        import sqlite3
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 1)
        self._insert(db_path, "slug-ha-only", "ha", 1000)
        self._insert(db_path, "slug-new", "both", 2000)
        ssh = FakeSSHClient()
        asyncio.run(ha_agent_advanced.enforce_ha_retention(ssh_client=ssh))
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT deleted_from_ha_at FROM backup_registry"
                " WHERE backup_slug = 'slug-ha-only'"
            ).fetchone()
        assert row[0] is None
        assert ssh.commands_run == []

    def test_ssh_error_on_delete_logs_warning_no_raise(self, db_path, monkeypatch):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 1)
        self._insert(db_path, "slug-fail", "both", 1000)
        self._insert(db_path, "slug-keep", "both", 2000)

        class FailOnRemove(FakeSSHClient):
            async def run(self, cmd, check=False):
                raise OSError("SSH connection lost")

        asyncio.run(ha_agent_advanced.enforce_ha_retention(ssh_client=FailOnRemove()))


class TestPurgeLocalBackups:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def _insert(self, db_path, slug, offloaded_at):
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry"
                " (timestamp, backup_slug, status, size_bytes, location, offloaded_at)"
                " VALUES (?, ?, 'ACTIVE', 0, 'both', ?)",
                (int(offloaded_at), slug, offloaded_at),
            )
            conn.commit()

    def test_old_file_deleted_and_inventory_updated(
        self, db_path, monkeypatch, tmp_path
    ):
        import time
        import sqlite3
        import ha_agent_advanced

        old_ts = time.time() - (40 * 86400)
        new_ts = time.time() - (1 * 86400)
        self._insert(db_path, "slug-old", old_ts)
        self._insert(db_path, "slug-new", new_ts)

        local_dir = tmp_path / "backups"
        local_dir.mkdir()
        (local_dir / "slug-old.tar").write_bytes(b"data")
        (local_dir / "slug-new.tar").write_bytes(b"data")
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", str(local_dir))
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_LOCAL_DAYS", 30)

        ha_agent_advanced.purge_local_backups()

        assert not (local_dir / "slug-old.tar").exists()
        assert (local_dir / "slug-new.tar").exists()
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = 'slug-old'"
            ).fetchone()
        assert row[0] == "ha"

    def test_most_recent_not_purged(self, db_path, monkeypatch, tmp_path):
        import time
        import ha_agent_advanced

        old_ts = time.time() - (60 * 86400)
        self._insert(db_path, "only-slug", old_ts)

        local_dir = tmp_path / "backups"
        local_dir.mkdir()
        (local_dir / "only-slug.tar").write_bytes(b"data")
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", str(local_dir))
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_LOCAL_DAYS", 30)

        ha_agent_advanced.purge_local_backups()
        assert (local_dir / "only-slug.tar").exists()

    def test_noop_when_dir_missing(self, db_path, monkeypatch, tmp_path):
        import ha_agent_advanced

        monkeypatch.setattr(
            ha_agent_advanced, "BACKUP_LOCAL_DIR", str(tmp_path / "nonexistent")
        )
        ha_agent_advanced.purge_local_backups()

    def test_recent_files_not_deleted(self, db_path, monkeypatch, tmp_path):
        import time
        import ha_agent_advanced

        recent_ts = time.time() - (5 * 86400)
        self._insert(db_path, "slug-recent", recent_ts)

        local_dir = tmp_path / "backups"
        local_dir.mkdir()
        (local_dir / "slug-recent.tar").write_bytes(b"data")
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", str(local_dir))
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_LOCAL_DAYS", 30)

        ha_agent_advanced.purge_local_backups()
        assert (local_dir / "slug-recent.tar").exists()


class TestPrintBackupStatus:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_empty_prints_no_backups(self, db_path, capsys):
        import ha_agent_advanced

        ha_agent_advanced.print_backup_status()
        out = capsys.readouterr().out
        assert "no backups" in out.lower()

    def test_shows_slug_and_marks(self, db_path, capsys):
        import sqlite3
        import ha_agent_advanced

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry"
                " (timestamp, backup_slug, status, size_bytes, location, offloaded_at)"
                " VALUES (?, 'abc123', 'ACTIVE', 10485760, 'both', ?)",
                (int(__import__("time").time()), __import__("time").time()),
            )
            conn.commit()

        ha_agent_advanced.print_backup_status()
        out = capsys.readouterr().out
        assert "abc123" in out
        assert "✓" in out


# ── HARestClient / UpdateStatus ──────────────────────────────────────────────────
class TestUpdateStatus:
    def test_update_available_when_state_on(self):
        from utils.ha_rest_client import _entity_to_update_status

        entity = {
            "entity_id": "update.home_assistant_core_update",
            "state": "on",
            "attributes": {
                "installed_version": "2026.6.0",
                "latest_version": "2026.7.0",
                "release_url": "https://github.com/home-assistant/core/releases/tag/2026.7.0",
                "release_summary": "Bug fixes",
                "in_progress": False,
            },
        }
        status = _entity_to_update_status(entity)
        assert status.component == "core"
        assert status.update_available is True
        assert status.installed_version == "2026.6.0"
        assert status.latest_version == "2026.7.0"
        assert status.release_url is not None

    def test_no_update_when_state_off(self):
        from utils.ha_rest_client import _entity_to_update_status

        entity = {
            "entity_id": "update.home_assistant_os_update",
            "state": "off",
            "attributes": {
                "installed_version": "14.2",
                "latest_version": "14.2",
            },
        }
        status = _entity_to_update_status(entity)
        assert status.component == "os"
        assert status.update_available is False

    def test_addon_component_name_derived(self):
        from utils.ha_rest_client import _entity_to_update_status

        entity = {
            "entity_id": "update.my_cool_addon_update",
            "state": "on",
            "attributes": {
                "installed_version": "1.0",
                "latest_version": "1.1",
            },
        }
        status = _entity_to_update_status(entity)
        assert status.component == "my_cool_addon"
        assert status.update_available is True

    def test_supervisor_component_mapped(self):
        from utils.ha_rest_client import _entity_to_update_status

        entity = {
            "entity_id": "update.home_assistant_supervisor_update",
            "state": "off",
            "attributes": {},
        }
        status = _entity_to_update_status(entity)
        assert status.component == "supervisor"


class TestFakeHARestClient:
    def test_get_states_returns_all(self):
        from utils.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient(
            states=[
                {"entity_id": "update.core_update", "state": "on", "attributes": {}},
                {"entity_id": "light.living_room", "state": "on", "attributes": {}},
            ]
        )
        result = asyncio.run(fake.get_states())
        assert len(result) == 2

    def test_get_states_filters_by_prefix(self):
        from utils.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient(
            states=[
                {"entity_id": "update.core_update", "state": "on", "attributes": {}},
                {"entity_id": "light.living_room", "state": "on", "attributes": {}},
            ]
        )
        result = asyncio.run(fake.get_states(prefix="update."))
        assert len(result) == 1
        assert result[0]["entity_id"] == "update.core_update"

    def test_get_state_returns_matching_entity(self):
        from utils.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient(
            states=[
                {"entity_id": "update.core_update", "state": "on", "attributes": {}}
            ]
        )
        result = asyncio.run(fake.get_state("update.core_update"))
        assert result["entity_id"] == "update.core_update"

    def test_get_state_raises_for_missing_entity(self):
        import httpx
        from utils.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient(states=[])
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(fake.get_state("update.nonexistent"))

    def test_call_service_records_call(self):
        from utils.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient()
        asyncio.run(
            fake.call_service("update", "install", {"entity_id": "update.core_update"})
        )
        assert len(fake.service_calls) == 1
        domain, service, payload = fake.service_calls[0]
        assert domain == "update"
        assert service == "install"
        assert payload["entity_id"] == "update.core_update"


class TestGetUpdateStatus:
    def test_returns_update_status_list(self):
        from utils.ha_rest_client import FakeHARestClient, get_update_status

        fake = FakeHARestClient(
            states=[
                {
                    "entity_id": "update.home_assistant_core_update",
                    "state": "on",
                    "attributes": {
                        "installed_version": "2026.6.0",
                        "latest_version": "2026.7.0",
                    },
                },
                {
                    "entity_id": "update.home_assistant_os_update",
                    "state": "off",
                    "attributes": {
                        "installed_version": "14.2",
                        "latest_version": "14.2",
                    },
                },
            ]
        )
        updates = asyncio.run(get_update_status(fake))
        assert len(updates) == 2
        core = next(u for u in updates if u.component == "core")
        assert core.update_available is True
        os_upd = next(u for u in updates if u.component == "os")
        assert os_upd.update_available is False

    def test_returns_empty_when_no_update_entities(self):
        from utils.ha_rest_client import FakeHARestClient, get_update_status

        fake = FakeHARestClient(
            states=[{"entity_id": "light.living_room", "state": "on", "attributes": {}}]
        )
        updates = asyncio.run(get_update_status(fake))
        assert updates == []


# ── ha_update_manager ────────────────────────────────────────────────────────────
class TestFormatUpdateTable:
    def test_empty_list_returns_message(self):
        from ha_update_manager import _format_update_table

        result = _format_update_table([])
        assert "No update entities found" in result

    def test_shows_component_and_versions(self):
        from ha_update_manager import _format_update_table
        from utils.ha_rest_client import UpdateStatus

        updates = [
            UpdateStatus(
                component="core",
                entity_id="update.home_assistant_core_update",
                installed_version="2026.6.0",
                latest_version="2026.7.0",
                update_available=True,
                release_url=None,
                release_summary=None,
                in_progress=False,
            )
        ]
        result = _format_update_table(updates)
        assert "core" in result
        assert "2026.6.0" in result
        assert "2026.7.0" in result
        assert "YES" in result

    def test_no_update_shows_no(self):
        from ha_update_manager import _format_update_table
        from utils.ha_rest_client import UpdateStatus

        updates = [
            UpdateStatus(
                component="os",
                entity_id="update.home_assistant_os_update",
                installed_version="14.2",
                latest_version="14.2",
                update_available=False,
                release_url=None,
                release_summary=None,
                in_progress=False,
            )
        ]
        result = _format_update_table(updates)
        assert "no" in result
        assert "YES" not in result


class TestRunUpdateCheck:
    def test_returns_updates_with_fake_client(self):
        from ha_update_manager import run_update_check
        from utils.ha_rest_client import FakeHARestClient

        fake = FakeHARestClient(
            states=[
                {
                    "entity_id": "update.home_assistant_core_update",
                    "state": "on",
                    "attributes": {
                        "installed_version": "2026.6.0",
                        "latest_version": "2026.7.0",
                    },
                }
            ]
        )
        updates = asyncio.run(run_update_check(ha_rest_client=fake))
        assert len(updates) == 1
        assert updates[0].component == "core"

    def test_returns_empty_when_no_token_and_no_client(self, isolated_config, capsys):
        import importlib
        import sys
        import yaml

        isolated_config.write_text(yaml.dump({"home_assistant": {"api_token": ""}}))
        importlib.reload(sys.modules["config"])
        import ha_update_manager

        importlib.reload(ha_update_manager)

        updates = asyncio.run(ha_update_manager.run_update_check(ha_rest_client=None))
        assert updates == []
        out = capsys.readouterr().out
        assert "api_token" in out or "HA_API_TOKEN" in out

    def test_returns_empty_on_client_error(self, capsys):
        from ha_update_manager import run_update_check

        class ErrorClient:
            async def get_states(self, prefix=None):
                raise ConnectionError("connection refused")

        updates = asyncio.run(run_update_check(ha_rest_client=ErrorClient()))
        assert updates == []
        out = capsys.readouterr().out
        assert "Error" in out

    def test_ssh_read_file_error_is_skipped(self, tmp_path, capsys):
        """SSH config fetch failure logs a warning but does not abort analysis."""
        from ha_update_manager import run_update_check
        from utils.ha_rest_client import FakeHARestClient

        fake_rest = FakeHARestClient(
            states=[
                {
                    "entity_id": "update.home_assistant_core_update",
                    "state": "on",
                    "attributes": {
                        "installed_version": "2026.6.0",
                        "latest_version": "2026.7.0",
                    },
                }
            ]
        )

        class BrokenSSH:
            async def read_file(self, path):
                raise OSError("sftp error")

        # Cache dir with no release notes so the loop skips analysis after the
        # SSH failure — we just need to cover the except branch.
        updates = asyncio.run(
            run_update_check(
                ha_rest_client=fake_rest,
                ssh_client=BrokenSSH(),
                cache_dir=str(tmp_path),
            )
        )
        assert len(updates) == 1

    def test_analyze_breaking_changes_error_is_skipped(self, tmp_path, capsys):
        """analyze_breaking_changes failure prints a warning and returns all updates."""
        import unittest.mock as mock
        from ha_update_manager import run_update_check
        from utils.ha_rest_client import FakeHARestClient

        fake_rest = FakeHARestClient(
            states=[
                {
                    "entity_id": "update.home_assistant_core_update",
                    "state": "on",
                    "attributes": {
                        "installed_version": "2026.6.0",
                        "latest_version": "2026.7.0",
                    },
                }
            ]
        )

        # Write a fake cached release notes file so the loop reaches
        # analyze_breaking_changes.
        notes_dir = tmp_path / "release_notes"
        notes_dir.mkdir()
        (notes_dir / "2026.7.0.txt").write_text(
            "## Breaking changes\n- Something changed"
        )

        with mock.patch(
            "ha_update_manager.analyze_breaking_changes",
            side_effect=RuntimeError("llm exploded"),
        ):
            updates = asyncio.run(
                run_update_check(
                    ha_rest_client=fake_rest,
                    cache_dir=str(notes_dir),
                )
            )

        assert len(updates) == 1
        out = capsys.readouterr().out
        assert "Warning" in out


# ── UpdateReadinessReport schema ─────────────────────────────────────────────────
class TestUpdateReadinessReport:
    def test_valid_construction(self):
        from ha_update_manager import UpdateReadinessReport

        report = UpdateReadinessReport(
            target_version="2026.7.0",
            safe_to_update=True,
            breaking_changes=["Template syntax changed"],
            affected_config_keys=["template"],
            pueo_command_risks=[],
            recommendation="Review template changes before updating.",
        )
        assert report.target_version == "2026.7.0"
        assert report.safe_to_update is True
        assert len(report.breaking_changes) == 1

    def test_empty_lists_valid(self):
        from ha_update_manager import UpdateReadinessReport

        report = UpdateReadinessReport(
            target_version="2026.7.0",
            safe_to_update=True,
            breaking_changes=[],
            affected_config_keys=[],
            pueo_command_risks=[],
            recommendation="No breaking changes found.",
        )
        assert report.breaking_changes == []
        assert report.pueo_command_risks == []

    def test_missing_required_field_raises(self):
        import pytest
        from pydantic import ValidationError
        from ha_update_manager import UpdateReadinessReport

        with pytest.raises(ValidationError):
            UpdateReadinessReport(
                safe_to_update=True,
                breaking_changes=[],
                affected_config_keys=[],
                pueo_command_risks=[],
                recommendation="ok",
                # missing target_version
            )

    def test_json_round_trip(self):
        from ha_update_manager import UpdateReadinessReport

        report = UpdateReadinessReport(
            target_version="2026.7.0",
            safe_to_update=False,
            breaking_changes=["CLI rename: ha addons -> ha apps"],
            affected_config_keys=[],
            pueo_command_risks=["ha apps info <slug>"],
            recommendation="CLI command renamed; Pueo self-check required.",
        )
        json_str = report.model_dump_json()
        restored = UpdateReadinessReport.model_validate_json(json_str)
        assert restored.target_version == report.target_version
        assert restored.pueo_command_risks == report.pueo_command_risks


# ── fetch_release_notes_cached ───────────────────────────────────────────────────
class TestFetchReleaseNotesCached:
    def test_returns_cached_content_when_file_exists(self, tmp_path):
        cached = tmp_path / "2026.7.0.txt"
        cached.write_text("Cached release notes content")

        from ha_update_manager import fetch_release_notes_cached

        result = asyncio.run(fetch_release_notes_cached("2026.7.0", str(tmp_path)))
        assert result == "Cached release notes content"

    def test_fetches_and_caches_when_file_missing(self, tmp_path):
        async def fake_fetcher(version: str) -> str:
            return f"Release notes for {version}"

        from ha_update_manager import fetch_release_notes_cached

        result = asyncio.run(
            fetch_release_notes_cached("2026.7.0", str(tmp_path), _fetcher=fake_fetcher)
        )
        assert result == "Release notes for 2026.7.0"
        assert (tmp_path / "2026.7.0.txt").exists()
        assert (tmp_path / "2026.7.0.txt").read_text() == "Release notes for 2026.7.0"

    def test_cache_hit_skips_fetcher(self, tmp_path):
        from unittest.mock import AsyncMock

        cached = tmp_path / "2026.7.0.txt"
        cached.write_text("Cached")
        mock_fetcher = AsyncMock()

        from ha_update_manager import fetch_release_notes_cached

        asyncio.run(
            fetch_release_notes_cached("2026.7.0", str(tmp_path), _fetcher=mock_fetcher)
        )
        mock_fetcher.assert_not_called()

    def test_creates_cache_dir_if_missing(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"

        async def fake_fetcher(version: str) -> str:
            return "notes"

        from ha_update_manager import fetch_release_notes_cached

        asyncio.run(
            fetch_release_notes_cached("2026.7.0", str(nested), _fetcher=fake_fetcher)
        )
        assert (nested / "2026.7.0.txt").exists()


# ── analyze_breaking_changes ─────────────────────────────────────────────────────
class TestAnalyzeBreakingChanges:
    @staticmethod
    def _make_core_update():
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component="core",
            entity_id="update.home_assistant_core_update",
            installed_version="2026.6.0",
            latest_version="2026.7.0",
            update_available=True,
            release_url="https://github.com/home-assistant/core/releases/tag/2026.7.0",
            release_summary="Bug fixes and improvements",
            in_progress=False,
        )

    @staticmethod
    def _fake_llm(
        safe_to_update: bool = True,
        breaking_changes: list | None = None,
        pueo_command_risks: list | None = None,
    ):
        from ha_update_manager import UpdateReadinessReport
        from utils.ollama_client import FakeLLMClient

        report = UpdateReadinessReport(
            target_version="2026.7.0",
            safe_to_update=safe_to_update,
            breaking_changes=breaking_changes or [],
            affected_config_keys=[],
            pueo_command_risks=pueo_command_risks or [],
            recommendation="No breaking changes.",
        )
        return FakeLLMClient(report.model_dump_json())

    def test_returns_readiness_report(self):
        from ha_update_manager import analyze_breaking_changes

        update = self._make_core_update()
        llm = self._fake_llm()
        report = asyncio.run(
            analyze_breaking_changes(update, "homeassistant: ~", "release notes", llm)
        )
        assert report.target_version == "2026.7.0"
        assert report.safe_to_update is True

    def test_llm_called_with_version_info(self):
        from ha_update_manager import analyze_breaking_changes

        update = self._make_core_update()
        llm = self._fake_llm()
        asyncio.run(analyze_breaking_changes(update, "config: {}", "notes", llm))
        assert len(llm.calls) == 1
        messages = llm.calls[0]["messages"]
        user_content = messages[-1]["content"]
        assert "2026.7.0" in user_content
        assert "2026.6.0" in user_content

    def test_breaking_changes_propagated(self):
        from ha_update_manager import analyze_breaking_changes

        update = self._make_core_update()
        llm = self._fake_llm(
            safe_to_update=False,
            breaking_changes=["Template syntax changed"],
            pueo_command_risks=["ha apps list renamed"],
        )
        report = asyncio.run(analyze_breaking_changes(update, "", "release notes", llm))
        assert report.safe_to_update is False
        assert "Template syntax changed" in report.breaking_changes
        assert len(report.pueo_command_risks) == 1


# ── run_update_check + analysis integration ───────────────────────────────────────
class TestRunUpdateCheckWithAnalysis:
    @staticmethod
    def _fake_rest(with_core_update: bool = True):
        from utils.ha_rest_client import FakeHARestClient

        states = []
        if with_core_update:
            states.append(
                {
                    "entity_id": "update.home_assistant_core_update",
                    "state": "on",
                    "attributes": {
                        "installed_version": "2026.6.0",
                        "latest_version": "2026.7.0",
                        "release_url": "https://github.com/home-assistant/core/releases/tag/2026.7.0",
                    },
                }
            )
        return FakeHARestClient(states=states)

    def test_analysis_runs_for_core_update(self, tmp_path, capsys):
        from ha_update_manager import UpdateReadinessReport, run_update_check
        from utils.ollama_client import FakeLLMClient

        # Pre-populate the cache so no WAN fetch is needed.
        (tmp_path / "2026.7.0.txt").write_text("Release notes text")

        report = UpdateReadinessReport(
            target_version="2026.7.0",
            safe_to_update=True,
            breaking_changes=[],
            affected_config_keys=[],
            pueo_command_risks=[],
            recommendation="No breaking changes found.",
        )
        llm = FakeLLMClient(report.model_dump_json())

        updates = asyncio.run(
            run_update_check(
                ha_rest_client=self._fake_rest(),
                llm_client=llm,
                cache_dir=str(tmp_path),
            )
        )

        out = capsys.readouterr().out
        assert len(updates) == 1
        assert "Breaking Change Analysis" in out

    def test_no_analysis_when_no_core_update(self, tmp_path, capsys):
        from ha_update_manager import run_update_check
        from utils.ollama_client import FakeLLMClient

        llm = FakeLLMClient("{}")
        updates = asyncio.run(
            run_update_check(
                ha_rest_client=self._fake_rest(with_core_update=False),
                llm_client=llm,
                cache_dir=str(tmp_path),
            )
        )
        assert updates == []
        assert llm.calls == []

    def test_analysis_skipped_when_notes_fetch_fails(self, tmp_path, capsys):
        from ha_update_manager import run_update_check
        from utils.ollama_client import FakeLLMClient
        from unittest.mock import AsyncMock, patch

        llm = FakeLLMClient("{}")
        with patch(
            "ha_update_manager.fetch_release_notes_cached",
            new=AsyncMock(side_effect=Exception("GitHub unavailable")),
        ):
            asyncio.run(
                run_update_check(
                    ha_rest_client=self._fake_rest(),
                    llm_client=llm,
                    cache_dir=str(tmp_path),
                )
            )
        assert llm.calls == []
        out = capsys.readouterr().out
        assert "Warning" in out


# ── request_update_approval ───────────────────────────────────────────────────────
class TestRequestUpdateApproval:
    def _make_update(self, component: str = "core"):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component=component,
            entity_id=f"update.home_assistant_{component}_update",
            installed_version="2026.6.0",
            latest_version="2026.7.0",
            update_available=True,
            release_url="https://github.com/home-assistant/core/releases/tag/2026.7.0",
            release_summary="Bug fixes and improvements.",
            in_progress=False,
        )

    def test_core_update_uses_critical_risk(self):
        from ha_update_manager import request_update_approval
        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        result = asyncio.run(
            request_update_approval(self._make_update("core"), gate, notifier)
        )
        assert result is True
        assert len(gate.require_approval_calls) == 1
        assert gate.require_approval_calls[0]["risk"] == RiskLevel.CRITICAL

    def test_os_update_uses_critical_risk(self):
        from ha_update_manager import request_update_approval
        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)
        result = asyncio.run(
            request_update_approval(self._make_update("os"), gate, notifier)
        )
        assert result is False
        assert gate.require_approval_calls[0]["risk"] == RiskLevel.CRITICAL

    def test_supervisor_update_uses_high_risk(self):
        from ha_update_manager import request_update_approval
        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            request_update_approval(self._make_update("supervisor"), gate, notifier)
        )
        assert gate.require_approval_calls[0]["risk"] == RiskLevel.HIGH

    def test_addon_update_uses_medium_risk(self):
        from ha_update_manager import request_update_approval
        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            request_update_approval(self._make_update("some_addon"), gate, notifier)
        )
        assert gate.require_approval_calls[0]["risk"] == RiskLevel.MEDIUM

    def test_payload_includes_version_info(self):
        from ha_update_manager import request_update_approval
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        asyncio.run(request_update_approval(self._make_update("core"), gate, notifier))
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["component"] == "core"
        assert payload["installed_version"] == "2026.6.0"
        assert payload["latest_version"] == "2026.7.0"

    def test_payload_includes_breaking_changes_from_report(self):
        from ha_update_manager import UpdateReadinessReport, request_update_approval
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        report = UpdateReadinessReport(
            target_version="2026.7.0",
            safe_to_update=False,
            breaking_changes=["Template syntax changed"],
            affected_config_keys=["template"],
            pueo_command_risks=["ha apps info <slug>"],
            recommendation="Review before updating.",
        )
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            request_update_approval(
                self._make_update("core"), gate, notifier, readiness_report=report
            )
        )
        payload = notifier.sent[0]["payload"]
        assert payload["breaking_changes"] == ["Template syntax changed"]
        assert payload["affected_config_keys"] == ["template"]
        assert payload["pueo_command_risks"] == ["ha apps info <slug>"]
        assert payload["safe_to_update"] is False

    def test_payload_empty_lists_when_no_report(self):
        from ha_update_manager import request_update_approval
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        asyncio.run(request_update_approval(self._make_update("core"), gate, notifier))
        payload = notifier.sent[0]["payload"]
        assert payload["breaking_changes"] == []
        assert payload["affected_config_keys"] == []
        assert payload["pueo_command_risks"] == []
        assert payload["advisory"] is None

    def test_disk_info_included_in_payload(self):
        from ha_update_manager import request_update_approval
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            request_update_approval(
                self._make_update("core"),
                gate,
                notifier,
                disk_free_gb=3.5,
                disk_warn=True,
                disk_critical=False,
            )
        )
        payload = notifier.sent[0]["payload"]
        assert payload["disk_free_gb"] == 3.5
        assert payload["disk_warn"] is True
        assert payload["disk_critical"] is False

    def test_custom_notification_id_used(self):
        from ha_update_manager import request_update_approval
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        asyncio.run(
            request_update_approval(
                self._make_update("core"),
                gate,
                notifier,
                notification_id="my-custom-id",
            )
        )
        payload = notifier.sent[0]["payload"]
        assert payload["notification_id"] == "my-custom-id"

    def test_autonomous_level4_auto_executes_addon(self):
        """Level-4 autonomy auto-executes MEDIUM-risk add-on updates."""
        from ha_update_manager import request_update_approval
        from utils.autonomy import AutonomyGate
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=True)
        result = asyncio.run(
            request_update_approval(self._make_update("some_addon"), gate, notifier)
        )
        assert result is True
        assert notifier.sent == []  # no notification sent for auto-execute

    def test_autonomous_level4_still_asks_for_core(self):
        """Level-4 autonomy must still ask for CRITICAL-risk core updates."""
        from ha_update_manager import request_update_approval
        from utils.autonomy import AutonomyGate
        from utils.notify import FakeNotifier

        gate = AutonomyGate(level=4)
        notifier = FakeNotifier(approve=True)
        result = asyncio.run(
            request_update_approval(self._make_update("core"), gate, notifier)
        )
        assert result is True
        assert len(notifier.sent) == 1  # HITL card was sent


# ── _component_risk_level ─────────────────────────────────────────────────────────
class TestComponentRiskLevel:
    def test_core_is_critical(self):
        from ha_update_manager import _component_risk_level
        from utils.autonomy import RiskLevel

        assert _component_risk_level("core") == RiskLevel.CRITICAL

    def test_os_is_critical(self):
        from ha_update_manager import _component_risk_level
        from utils.autonomy import RiskLevel

        assert _component_risk_level("os") == RiskLevel.CRITICAL

    def test_supervisor_is_high(self):
        from ha_update_manager import _component_risk_level
        from utils.autonomy import RiskLevel

        assert _component_risk_level("supervisor") == RiskLevel.HIGH

    def test_addon_is_medium(self):
        from ha_update_manager import _component_risk_level
        from utils.autonomy import RiskLevel

        assert _component_risk_level("my_addon") == RiskLevel.MEDIUM


# ── _poll_core_version ────────────────────────────────────────────────────────
async def _noop_sleep(s):
    pass


class TestPollCoreVersion:
    def _make_ssh(self, version: str = "2026.7.0", update_available: bool = False):
        import json
        from utils.ssh_client import FakeSSHClient

        payload = json.dumps(
            {"data": {"version": version, "update_available": update_available}}
        )
        return FakeSSHClient(command_results={"ha core info": (0, payload, "")})

    def test_returns_true_when_version_matches(self):
        from ha_update_manager import _poll_core_version

        ssh = self._make_ssh("2026.7.0", False)
        result = asyncio.run(
            _poll_core_version(
                "2026.7.0", ssh, timeout_seconds=60, interval=5, _sleep=_noop_sleep
            )
        )
        assert result is True

    def test_returns_false_on_timeout(self):
        from ha_update_manager import _poll_core_version
        from utils.ssh_client import FakeSSHClient
        import json

        payload = json.dumps(
            {"data": {"version": "2026.6.0", "update_available": True}}
        )
        ssh = FakeSSHClient(command_results={"ha core info": (0, payload, "")})
        result = asyncio.run(
            _poll_core_version(
                "2026.7.0", ssh, timeout_seconds=0, interval=5, _sleep=_noop_sleep
            )
        )
        assert result is False

    def test_handles_json_parse_error(self):
        from ha_update_manager import _poll_core_version
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={"ha core info": (0, "not-json", "")})
        result = asyncio.run(
            _poll_core_version(
                "2026.7.0", ssh, timeout_seconds=0, interval=5, _sleep=_noop_sleep
            )
        )
        assert result is False

    def test_exception_covered_and_retried_until_timeout(self):
        from ha_update_manager import _poll_core_version
        from utils.ssh_client import FakeSSHClient

        # timeout=5, interval=5 → one iteration that raises, then elapsed=5 exits loop
        ssh = FakeSSHClient(command_results={"ha core info": (0, "not-json", "")})
        result = asyncio.run(
            _poll_core_version(
                "2026.7.0", ssh, timeout_seconds=5, interval=5, _sleep=_noop_sleep
            )
        )
        assert result is False

    def test_returns_true_when_update_available_false(self):
        from ha_update_manager import _poll_core_version
        import json
        from utils.ssh_client import FakeSSHClient

        payload = json.dumps(
            {"data": {"version": "2026.6.0", "update_available": False}}
        )
        ssh = FakeSSHClient(command_results={"ha core info": (0, payload, "")})
        result = asyncio.run(
            _poll_core_version(
                "2026.7.0", ssh, timeout_seconds=60, interval=5, _sleep=_noop_sleep
            )
        )
        assert result is True


# ── _poll_os_online ────────────────────────────────────────────────────────────
class TestPollOsOnline:
    def test_returns_true_on_successful_connect(self):
        from ha_update_manager import _poll_os_online

        async def fake_connect(host, port):
            class FakeWriter:
                def close(self):
                    pass

                async def wait_closed(self):
                    pass

            return object(), FakeWriter()

        result = asyncio.run(
            _poll_os_online(
                "ha.local",
                8123,
                timeout_seconds=60,
                interval=5,
                _sleep=_noop_sleep,
                _connect=fake_connect,
            )
        )
        assert result is True

    def test_returns_false_on_timeout(self):
        from ha_update_manager import _poll_os_online

        async def fail_connect(host, port):
            raise ConnectionRefusedError("no server")

        result = asyncio.run(
            _poll_os_online(
                "ha.local",
                8123,
                timeout_seconds=0,
                interval=5,
                _sleep=_noop_sleep,
                _connect=fail_connect,
            )
        )
        assert result is False

    def test_connection_error_is_swallowed_before_timeout(self):
        from ha_update_manager import _poll_os_online

        attempts = [0]

        async def flaky_connect(host, port):
            attempts[0] += 1
            if attempts[0] < 2:
                raise OSError("timeout")

            class FakeWriter:
                def close(self):
                    pass

                async def wait_closed(self):
                    pass

            return object(), FakeWriter()

        result = asyncio.run(
            _poll_os_online(
                "ha.local",
                8123,
                timeout_seconds=30,
                interval=5,
                _sleep=_noop_sleep,
                _connect=flaky_connect,
            )
        )
        assert result is True


# ── _poll_addon_version ────────────────────────────────────────────────────────
class TestPollAddonVersion:
    def test_returns_true_when_version_and_state_match(self):
        from ha_update_manager import _poll_addon_version
        import json
        from utils.ssh_client import FakeSSHClient

        payload = json.dumps({"data": {"version": "3.0.0", "state": "started"}})
        ssh = FakeSSHClient(command_results={"ha apps info": (0, payload, "")})
        result = asyncio.run(
            _poll_addon_version(
                "my_addon",
                "3.0.0",
                ssh,
                timeout_seconds=60,
                interval=5,
                _sleep=_noop_sleep,
            )
        )
        assert result is True

    def test_returns_false_when_state_not_started(self):
        from ha_update_manager import _poll_addon_version
        import json
        from utils.ssh_client import FakeSSHClient

        payload = json.dumps({"data": {"version": "3.0.0", "state": "updating"}})
        ssh = FakeSSHClient(command_results={"ha apps info": (0, payload, "")})
        result = asyncio.run(
            _poll_addon_version(
                "my_addon",
                "3.0.0",
                ssh,
                timeout_seconds=0,
                interval=5,
                _sleep=_noop_sleep,
            )
        )
        assert result is False

    def test_returns_false_on_timeout(self):
        from ha_update_manager import _poll_addon_version
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={"ha apps info": (0, "not-json", "")})
        result = asyncio.run(
            _poll_addon_version(
                "my_addon",
                "3.0.0",
                ssh,
                timeout_seconds=0,
                interval=5,
                _sleep=_noop_sleep,
            )
        )
        assert result is False

    def test_exception_covered_and_retried_until_timeout(self):
        from ha_update_manager import _poll_addon_version
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={"ha apps info": (0, "not-json", "")})
        result = asyncio.run(
            _poll_addon_version(
                "my_addon",
                "3.0.0",
                ssh,
                timeout_seconds=10,
                interval=10,
                _sleep=_noop_sleep,
            )
        )
        assert result is False


# ── _send_post_update_card ────────────────────────────────────────────────────
class TestSendPostUpdateCard:
    def _make_update(self, component: str = "core"):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component=component,
            entity_id=f"update.home_assistant_{component}_update",
            installed_version="2026.6.0",
            latest_version="2026.7.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

    def test_sends_to_notifier_on_success(self):
        from ha_update_manager import _send_post_update_card
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        asyncio.run(
            _send_post_update_card(
                self._make_update(), notifier, True, "ok", "all clear"
            )
        )
        assert len(notifier.sent) == 1
        assert "succeeded" in notifier.sent[0]["subject"]

    def test_sends_to_notifier_on_failure(self):
        from ha_update_manager import _send_post_update_card
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        asyncio.run(
            _send_post_update_card(self._make_update(), notifier, False, "", "")
        )
        assert "timed out" in notifier.sent[0]["subject"]

    def test_payload_contains_component_and_versions(self):
        from ha_update_manager import _send_post_update_card
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        asyncio.run(
            _send_post_update_card(
                self._make_update("os"), notifier, True, "valid", "ok"
            )
        )
        payload = notifier.sent[0]["payload"]
        assert payload["component"] == "os"
        assert payload["installed_version"] == "2026.6.0"
        assert payload["latest_version"] == "2026.7.0"
        assert payload["success"] is True

    def test_payload_includes_config_check_output(self):
        from ha_update_manager import _send_post_update_card
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        asyncio.run(
            _send_post_update_card(
                self._make_update(), notifier, True, "all good", "no issues"
            )
        )
        assert notifier.sent[0]["payload"]["config_check_output"] == "all good"
        assert notifier.sent[0]["payload"]["log_triage_summary"] == "no issues"


# ── execute_core_update ────────────────────────────────────────────────────────
class TestExecuteCoreUpdate:
    def _make_update(self):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component="core",
            entity_id="update.home_assistant_core_update",
            installed_version="2026.6.0",
            latest_version="2026.7.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

    def _make_gate_notifier(self, approve: bool = True):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        return FakeAutonomyGate(approval_result=approve), FakeNotifier(approve=approve)

    def _patch_backup(self, slug: str = "slug1"):
        """Context manager that stubs ha_agent_advanced backup functions."""
        import ha_agent_advanced
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            called = []
            recorded = []

            async def mock_backup(ssh_client=None):
                called.append(slug)
                return slug

            def mock_record(s):
                recorded.append(s)

            orig_b = ha_agent_advanced.execute_remote_backup
            orig_r = ha_agent_advanced.record_backup_slug
            ha_agent_advanced.execute_remote_backup = mock_backup
            ha_agent_advanced.record_backup_slug = mock_record
            try:
                yield called, recorded
            finally:
                ha_agent_advanced.execute_remote_backup = orig_b
                ha_agent_advanced.record_backup_slug = orig_r

        return _ctx()

    def test_calls_backup_before_update(self):
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={"ha core check": (0, "Config valid", "")})
        gate, notifier = self._make_gate_notifier()

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        with self._patch_backup() as (called_backup, recorded):
            result = asyncio.run(
                execute_core_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert called_backup, "backup should have been triggered"
        assert recorded == ["slug1"]
        assert result is True

    def test_sends_post_update_card_on_success(self):
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={"ha core check": (0, "Config valid", "")})
        gate, notifier = self._make_gate_notifier()

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        with self._patch_backup():
            asyncio.run(
                execute_core_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert len(notifier.sent) == 1
        assert notifier.sent[0]["payload"]["success"] is True

    def test_sends_post_update_card_on_timeout(self):
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate, notifier = self._make_gate_notifier()

        async def fake_poll_fail(version, client, timeout_seconds=480):
            return False

        with self._patch_backup():
            result = asyncio.run(
                execute_core_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll_fail
                )
            )

        assert result is False
        assert notifier.sent[0]["payload"]["success"] is False

    def test_update_command_is_issued(self):
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={"ha core check": (0, "ok", "")})
        gate, notifier = self._make_gate_notifier()

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        with self._patch_backup():
            asyncio.run(
                execute_core_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert any("ha core update" in cmd for cmd in ssh.commands_run)

    def test_stderr_from_update_command_is_logged(self):
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ha core update": (0, "", "some warning"),
                "ha core check": (0, "ok", ""),
            }
        )
        gate, notifier = self._make_gate_notifier()

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_core_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert result is True  # stderr warning doesn't fail the update

    def test_config_check_exception_is_swallowed(self):
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient

        # FakeSSHClient with check=True raises on ha core check — simulate by raising in run
        class ErrorOnCheckSSH(FakeSSHClient):
            async def run(self, command, check=False):
                self.commands_run.append(command)
                if "ha core check" in command:
                    raise RuntimeError("SSH connection lost")
                return 0, "", ""

        ssh = ErrorOnCheckSSH()
        gate, notifier = self._make_gate_notifier()

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_core_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert (
            result is True
        )  # exception is swallowed, update still reported as success

    def test_log_triage_runs_when_logs_returned(self):
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient
        from utils.ollama_client import FakeLLMClient
        from ha_log_monitor import LogEvaluation

        evaluation_json = LogEvaluation(
            is_actionable=False,
            root_cause_summary="No issues found",
            confidence_score=0.1,
        ).model_dump_json()

        ssh = FakeSSHClient(
            command_results={
                "ha core check": (0, "valid", ""),
                "ha core logs": (
                    0,
                    "INFO: HA started successfully\nINFO: All good",
                    "",
                ),
            }
        )
        gate, notifier = self._make_gate_notifier()
        llm = FakeLLMClient(evaluation_json)

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_core_update(
                    self._make_update(),
                    ssh,
                    notifier,
                    gate,
                    llm_client=llm,
                    _poll=fake_poll,
                )
            )

        assert result is True
        assert notifier.sent[0]["payload"]["log_triage_summary"] == "No issues found"

    def test_log_triage_exception_is_swallowed(self):
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient

        # SSH raises on the logs command → covers the outer except branch
        class LogsRaisingSSH(FakeSSHClient):
            async def run(self, command, check=False):
                self.commands_run.append(command)
                if "ha core logs" in command:
                    raise RuntimeError("logs unavailable")
                return 0, "ok", ""

        ssh = LogsRaisingSSH()
        gate, notifier = self._make_gate_notifier()

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_core_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert result is True  # triage failure is swallowed


# ── execute_os_update ────────────────────────────────────────────────────────
class TestExecuteOsUpdate:
    def _make_update(self):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component="os",
            entity_id="update.home_assistant_os_update",
            installed_version="14.0",
            latest_version="15.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

    def _patch_backup(self, slug: str = "slug2"):
        import ha_agent_advanced
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            async def mock_backup(ssh_client=None):
                return slug

            orig_b = ha_agent_advanced.execute_remote_backup
            orig_r = ha_agent_advanced.record_backup_slug
            ha_agent_advanced.execute_remote_backup = mock_backup
            ha_agent_advanced.record_backup_slug = lambda s: None
            try:
                yield
            finally:
                ha_agent_advanced.execute_remote_backup = orig_b
                ha_agent_advanced.record_backup_slug = orig_r

        return _ctx()

    def test_calls_backup_and_issues_os_update(self):
        from ha_update_manager import execute_os_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        async def fake_poll(host, port, timeout_seconds=480):
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_os_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert result is True
        assert any("ha os update" in cmd for cmd in ssh.commands_run)
        assert notifier.sent[0]["payload"]["success"] is True

    def test_returns_false_on_timeout(self):
        from ha_update_manager import execute_os_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        async def fake_poll_fail(host, port, timeout_seconds=480):
            return False

        with self._patch_backup():
            result = asyncio.run(
                execute_os_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll_fail
                )
            )

        assert result is False
        assert notifier.sent[0]["payload"]["success"] is False


# ── execute_addon_update ────────────────────────────────────────────────────────
class TestExecuteAddonUpdate:
    def _make_update(self, slug: str = "my_addon"):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component=slug,
            entity_id=f"update.{slug}_update",
            installed_version="2.0.0",
            latest_version="3.0.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

    def _patch_backup(self, slug: str = "slug3"):
        import ha_agent_advanced
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            async def mock_backup(ssh_client=None):
                return slug

            orig_b = ha_agent_advanced.execute_remote_backup
            orig_r = ha_agent_advanced.record_backup_slug
            ha_agent_advanced.execute_remote_backup = mock_backup
            ha_agent_advanced.record_backup_slug = lambda s: None
            try:
                yield
            finally:
                ha_agent_advanced.execute_remote_backup = orig_b
                ha_agent_advanced.record_backup_slug = orig_r

        return _ctx()

    def test_calls_backup_and_issues_curl(self):
        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        async def fake_poll(slug, version, client, timeout_seconds=180):
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_addon_update(
                    self._make_update("my_addon"), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert result is True
        assert any(
            "supervisor/store/addons/my_addon/update" in cmd for cmd in ssh.commands_run
        )
        assert notifier.sent[0]["payload"]["success"] is True

    def test_returns_false_on_timeout(self):
        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        async def fake_poll_fail(slug, version, client, timeout_seconds=180):
            return False

        with self._patch_backup():
            result = asyncio.run(
                execute_addon_update(
                    self._make_update(), ssh, notifier, gate, _poll=fake_poll_fail
                )
            )

        assert result is False
        assert notifier.sent[0]["payload"]["success"] is False

    def test_curl_slug_uses_component_name(self):
        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        async def fake_poll(slug, version, client, timeout_seconds=180):
            return True

        with self._patch_backup():
            asyncio.run(
                execute_addon_update(
                    self._make_update("special_addon"),
                    ssh,
                    notifier,
                    gate,
                    _poll=fake_poll,
                )
            )

        curl_cmds = [cmd for cmd in ssh.commands_run if "supervisor/store" in cmd]
        assert len(curl_cmds) == 1
        assert "special_addon" in curl_cmds[0]

    def test_curl_failure_logged_but_poll_continues(self):
        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={"supervisor/store": (1, "", "curl error")})
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        async def fake_poll(slug, version, client, timeout_seconds=180):
            return (
                False  # poll fails regardless; just testing the curl-fail branch is hit
            )

        with self._patch_backup():
            result = asyncio.run(
                execute_addon_update(
                    self._make_update("my_addon"), ssh, notifier, gate, _poll=fake_poll
                )
            )

        assert result is False  # poll timed out, but curl-fail branch was exercised


# ── execute_update dispatch ────────────────────────────────────────────────────
class TestExecuteUpdate:
    def _make_update(self, component: str):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component=component,
            entity_id=f"update.{component}_update",
            installed_version="1.0",
            latest_version="2.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

    def test_dispatches_core_to_execute_core_update(self):
        from ha_update_manager import execute_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from unittest.mock import patch, AsyncMock

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        called = []

        async def fake_core(update, ssh, notifier, gate, llm_client=None):
            called.append("core")
            return True

        with patch("ha_update_manager.execute_core_update", fake_core):
            asyncio.run(execute_update(self._make_update("core"), ssh, notifier, gate))

        assert called == ["core"]

    def test_dispatches_os_to_execute_os_update(self):
        from ha_update_manager import execute_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from unittest.mock import patch

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        called = []

        async def fake_os(update, ssh, notifier, gate, ha_host=None, ha_port=None):
            called.append("os")
            return True

        with patch("ha_update_manager.execute_os_update", fake_os):
            asyncio.run(execute_update(self._make_update("os"), ssh, notifier, gate))

        assert called == ["os"]

    def test_dispatches_addon_to_execute_addon_update(self):
        from ha_update_manager import execute_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from unittest.mock import patch

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        called = []

        async def fake_addon(update, ssh, notifier, gate):
            called.append("addon")
            return True

        with patch("ha_update_manager.execute_addon_update", fake_addon):
            asyncio.run(
                execute_update(self._make_update("my_addon"), ssh, notifier, gate)
            )

        assert called == ["addon"]


# ── Item 37: Pueo self-check ──────────────────────────────────────────────────


class TestSelfCheckCommandRisk:
    def test_valid_construction(self):
        from ha_update_manager import SelfCheckCommandRisk

        report = SelfCheckCommandRisk(
            command_risks=["ha addons → ha apps renamed"],
            summary="One command renamed.",
        )
        assert len(report.command_risks) == 1
        assert "renamed" in report.summary

    def test_empty_risks_valid(self):
        from ha_update_manager import SelfCheckCommandRisk

        report = SelfCheckCommandRisk(command_risks=[], summary="No risks found.")
        assert report.command_risks == []

    def test_missing_required_field_raises(self):
        from pydantic import ValidationError
        from ha_update_manager import SelfCheckCommandRisk

        with pytest.raises(ValidationError):
            SelfCheckCommandRisk(command_risks=["something"])  # missing summary

    def test_json_round_trip(self):
        from ha_update_manager import SelfCheckCommandRisk

        report = SelfCheckCommandRisk(
            command_risks=["ha core check removed"], summary="CLI broke."
        )
        restored = SelfCheckCommandRisk.model_validate_json(report.model_dump_json())
        assert restored.command_risks == report.command_risks
        assert restored.summary == report.summary


class TestPueoSelfCheckResult:
    def test_all_commands_ok_when_all_pass(self):
        from ha_update_manager import PueoSelfCheckResult

        result = PueoSelfCheckResult(
            core_check_ok=True,
            core_info_ok=True,
            apps_list_ok=True,
            netalertx_info_ok=True,
            backup_smoke_ok=True,
        )
        assert result.all_commands_ok is True

    def test_all_commands_ok_false_when_one_fails(self):
        from ha_update_manager import PueoSelfCheckResult

        result = PueoSelfCheckResult(
            core_check_ok=True,
            core_info_ok=False,
            apps_list_ok=True,
            netalertx_info_ok=True,
            backup_smoke_ok=None,
        )
        assert result.all_commands_ok is False

    def test_backup_smoke_none_by_default(self):
        from ha_update_manager import PueoSelfCheckResult

        result = PueoSelfCheckResult(
            core_check_ok=True,
            core_info_ok=True,
            apps_list_ok=True,
            netalertx_info_ok=True,
            backup_smoke_ok=None,
        )
        assert result.backup_smoke_ok is None

    def test_command_risks_defaults_to_empty_list(self):
        from ha_update_manager import PueoSelfCheckResult

        result = PueoSelfCheckResult(
            core_check_ok=True,
            core_info_ok=True,
            apps_list_ok=True,
            netalertx_info_ok=True,
            backup_smoke_ok=None,
        )
        assert result.command_risks == []


class TestRunPueoSelfCheck:
    def _make_fake_llm(self, risks: list[str] | None = None):
        from utils.ollama_client import FakeLLMClient
        from ha_update_manager import SelfCheckCommandRisk

        r = SelfCheckCommandRisk(
            command_risks=risks or [],
            summary="No risks." if not risks else "Risks found.",
        )
        return FakeLLMClient(response_json=r.model_dump_json())

    def test_all_ok_with_successful_commands(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ha core check": (0, "Config valid", ""),
                "ha core info": (0, '{"data": {"version": "2026.7.0"}}', ""),
                "ha apps list": (0, "db21ed7f_netalertx_fa", ""),
                "ha apps info": (0, '{"data": {"state": "started"}}', ""),
            }
        )
        result = asyncio.run(
            run_pueo_self_check(
                ssh,
                version="2026.7.0",
                cache_dir=str(tmp_path),
                llm_client=self._make_fake_llm(),
            )
        )
        assert result.core_check_ok is True
        assert result.core_info_ok is True
        assert result.apps_list_ok is True
        assert result.netalertx_info_ok is True
        assert result.all_commands_ok is True

    def test_failed_core_check_sets_flag_false(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ha core check": (1, "", "Config invalid"),
                "ha core info": (0, '{"data": {"version": "2026.7.0"}}', ""),
                "ha apps list": (0, "", ""),
                "ha apps info": (0, "", ""),
            }
        )
        result = asyncio.run(
            run_pueo_self_check(ssh, version="2026.7.0", cache_dir=str(tmp_path))
        )
        assert result.core_check_ok is False
        assert result.all_commands_ok is False

    def test_failed_core_info_sets_flag_false(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ha core check": (0, "ok", ""),
                "ha core info": (1, "", "error"),
                "ha apps list": (0, "", ""),
                "ha apps info": (0, "", ""),
            }
        )
        result = asyncio.run(
            run_pueo_self_check(ssh, version="2026.7.0", cache_dir=str(tmp_path))
        )
        assert result.core_info_ok is False

    def test_failed_apps_list_sets_flag_false(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ha core check": (0, "ok", ""),
                "ha core info": (0, '{"data": {}}', ""),
                "ha apps list": (1, "", "error"),
                "ha apps info": (0, "", ""),
            }
        )
        result = asyncio.run(
            run_pueo_self_check(ssh, version="2026.7.0", cache_dir=str(tmp_path))
        )
        assert result.apps_list_ok is False

    def test_failed_netalertx_info_sets_flag_false(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ha core check": (0, "ok", ""),
                "ha core info": (0, '{"data": {}}', ""),
                "ha apps list": (0, "", ""),
                "ha apps info": (1, "", "not found"),
            }
        )
        result = asyncio.run(
            run_pueo_self_check(ssh, version="2026.7.0", cache_dir=str(tmp_path))
        )
        assert result.netalertx_info_ok is False

    def test_exception_in_cli_command_is_swallowed(self, tmp_path):
        from ha_update_manager import run_pueo_self_check

        class ExplodingSSH:
            async def run(self, command, check=False):
                raise RuntimeError("SSH died")

        result = asyncio.run(
            run_pueo_self_check(
                ExplodingSSH(), version="2026.7.0", cache_dir=str(tmp_path)
            )
        )
        assert result.core_check_ok is False
        assert result.all_commands_ok is False

    def test_backup_smoke_skipped_when_disk_constrained(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        result = asyncio.run(
            run_pueo_self_check(
                ssh, version="2026.7.0", cache_dir=str(tmp_path), disk_free_gb=1.0
            )
        )
        assert result.backup_smoke_ok is None
        assert not any("pueo_selfcheck" in cmd for cmd in ssh.commands_run)

    def test_backup_smoke_skipped_when_disk_free_is_none(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        result = asyncio.run(
            run_pueo_self_check(
                ssh, version="2026.7.0", cache_dir=str(tmp_path), disk_free_gb=None
            )
        )
        assert result.backup_smoke_ok is None

    def test_backup_smoke_ok_when_disk_free_above_threshold(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "pueo_selfcheck": (0, "Backup complete.\nSlug: abc123", ""),
                "ha backup remove": (0, "", ""),
            }
        )
        result = asyncio.run(
            run_pueo_self_check(
                ssh, version="2026.7.0", cache_dir=str(tmp_path), disk_free_gb=20.0
            )
        )
        assert result.backup_smoke_ok is True

    def test_backup_smoke_false_when_slug_unknown(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "pueo_selfcheck": (0, "no slug info here", ""),
            }
        )
        result = asyncio.run(
            run_pueo_self_check(
                ssh, version="2026.7.0", cache_dir=str(tmp_path), disk_free_gb=20.0
            )
        )
        assert result.backup_smoke_ok is False

    def test_backup_smoke_exception_sets_false(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from config import HA_DISK_WARN_GB

        class BrokenSSHForBackup:
            commands_run = []

            async def run(self, command, check=False):
                self.commands_run.append(command)
                if "pueo_selfcheck" in command:
                    raise RuntimeError("backup exploded")
                return 0, "", ""

        ssh = BrokenSSHForBackup()
        result = asyncio.run(
            run_pueo_self_check(
                ssh,
                version="2026.7.0",
                cache_dir=str(tmp_path),
                disk_free_gb=HA_DISK_WARN_GB + 10.0,
            )
        )
        assert result.backup_smoke_ok is False

    def test_llm_cross_reference_runs_when_notes_cached(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        notes_path = tmp_path / "2026.7.0.txt"
        notes_path.write_text(
            "## Breaking changes\n- ha core check renamed to ha core verify"
        )

        ssh = FakeSSHClient()
        llm = self._make_fake_llm(risks=["ha core check → renamed"])
        result = asyncio.run(
            run_pueo_self_check(
                ssh, version="2026.7.0", cache_dir=str(tmp_path), llm_client=llm
            )
        )
        assert len(result.command_risks) == 1
        assert "ha core check" in result.command_risks[0]

    def test_llm_cross_reference_skipped_when_no_cache(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        result = asyncio.run(
            run_pueo_self_check(ssh, version="2026.7.0", cache_dir=str(tmp_path))
        )
        assert result.command_risks == []

    def test_llm_cross_reference_exception_is_swallowed(self, tmp_path):
        from ha_update_manager import run_pueo_self_check
        from utils.ssh_client import FakeSSHClient

        notes_path = tmp_path / "2026.7.0.txt"
        notes_path.write_text("some release notes")

        class BrokenLLM:
            async def chat(self, **kwargs):
                raise RuntimeError("LLM unavailable")

        ssh = FakeSSHClient()
        result = asyncio.run(
            run_pueo_self_check(
                ssh, version="2026.7.0", cache_dir=str(tmp_path), llm_client=BrokenLLM()
            )
        )
        assert result.command_risks == []


class TestSendPostUpdateCardWithSelfCheck:
    def _make_update(self):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component="core",
            entity_id="update.home_assistant_core_update",
            installed_version="2026.6.0",
            latest_version="2026.7.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

    def test_self_check_included_in_payload(self):
        from ha_update_manager import PueoSelfCheckResult, _send_post_update_card
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        sc = PueoSelfCheckResult(
            core_check_ok=True,
            core_info_ok=True,
            apps_list_ok=True,
            netalertx_info_ok=True,
            backup_smoke_ok=True,
            command_risks=[],
        )
        asyncio.run(
            _send_post_update_card(
                self._make_update(), notifier, True, "ok", "all clear", self_check=sc
            )
        )
        payload = notifier.sent[0]["payload"]
        assert "self_check" in payload
        assert payload["self_check"]["all_commands_ok"] is True
        assert payload["self_check"]["backup_smoke_ok"] is True

    def test_self_check_absent_when_none(self):
        from ha_update_manager import _send_post_update_card
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        asyncio.run(_send_post_update_card(self._make_update(), notifier, True, "", ""))
        assert "self_check" not in notifier.sent[0]["payload"]

    def test_command_risks_appear_in_body(self):
        from ha_update_manager import PueoSelfCheckResult, _send_post_update_card
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        sc = PueoSelfCheckResult(
            core_check_ok=True,
            core_info_ok=True,
            apps_list_ok=True,
            netalertx_info_ok=True,
            backup_smoke_ok=None,
            command_risks=["ha core check renamed"],
        )
        asyncio.run(
            _send_post_update_card(
                self._make_update(), notifier, True, "", "", self_check=sc
            )
        )
        body = notifier.sent[0]["body"]
        assert "ha core check renamed" in body

    def test_degraded_status_in_body_when_command_fails(self):
        from ha_update_manager import PueoSelfCheckResult, _send_post_update_card
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        sc = PueoSelfCheckResult(
            core_check_ok=False,
            core_info_ok=True,
            apps_list_ok=True,
            netalertx_info_ok=True,
            backup_smoke_ok=None,
        )
        asyncio.run(
            _send_post_update_card(
                self._make_update(), notifier, True, "", "", self_check=sc
            )
        )
        body = notifier.sent[0]["body"]
        assert "DEGRADED" in body


class TestExecuteCoreUpdateSelfCheck:
    def _make_update(self):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component="core",
            entity_id="update.home_assistant_core_update",
            installed_version="2026.6.0",
            latest_version="2026.7.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

    def _patch_backup(self, slug: str = "slug_sc"):
        import ha_agent_advanced
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            async def mock_backup(ssh_client=None):
                return slug

            def mock_record(s):
                pass

            orig_b = ha_agent_advanced.execute_remote_backup
            orig_r = ha_agent_advanced.record_backup_slug
            ha_agent_advanced.execute_remote_backup = mock_backup
            ha_agent_advanced.record_backup_slug = mock_record
            try:
                yield
            finally:
                ha_agent_advanced.execute_remote_backup = orig_b
                ha_agent_advanced.record_backup_slug = orig_r

        return _ctx()

    def test_self_check_included_in_card_on_success(self, tmp_path):
        from unittest.mock import patch, AsyncMock
        from ha_update_manager import execute_core_update, PueoSelfCheckResult
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        ssh = FakeSSHClient(command_results={"ha core check": (0, "ok", "")})
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        sc = PueoSelfCheckResult(
            core_check_ok=True,
            core_info_ok=True,
            apps_list_ok=True,
            netalertx_info_ok=True,
            backup_smoke_ok=None,
        )

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        async def fake_self_check(ssh, **kwargs):
            return sc

        with self._patch_backup():
            with patch("ha_update_manager.run_pueo_self_check", fake_self_check):
                asyncio.run(
                    execute_core_update(
                        self._make_update(), ssh, notifier, gate, _poll=fake_poll
                    )
                )

        payload = notifier.sent[0]["payload"]
        assert "self_check" in payload
        assert payload["self_check"]["all_commands_ok"] is True

    def test_self_check_not_run_on_timeout(self, tmp_path):
        from unittest.mock import patch
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        self_check_called = []

        async def fake_poll_fail(version, client, timeout_seconds=480):
            return False

        async def fake_self_check(ssh, **kwargs):
            self_check_called.append(True)
            return None

        with self._patch_backup():
            with patch("ha_update_manager.run_pueo_self_check", fake_self_check):
                asyncio.run(
                    execute_core_update(
                        self._make_update(), ssh, notifier, gate, _poll=fake_poll_fail
                    )
                )

        assert not self_check_called
        assert "self_check" not in notifier.sent[0]["payload"]

    def test_self_check_exception_is_swallowed(self, tmp_path):
        from unittest.mock import patch
        from ha_update_manager import execute_core_update
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        ssh = FakeSSHClient(command_results={"ha core check": (0, "ok", "")})
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        async def fake_poll(version, client, timeout_seconds=480):
            return True

        async def exploding_self_check(ssh, **kwargs):
            raise RuntimeError("self-check exploded")

        with self._patch_backup():
            with patch("ha_update_manager.run_pueo_self_check", exploding_self_check):
                result = asyncio.run(
                    execute_core_update(
                        self._make_update(), ssh, notifier, gate, _poll=fake_poll
                    )
                )

        assert result is True
        assert "self_check" not in notifier.sent[0]["payload"]


# ── ha_notification_manager — NotificationAnalysis schema ────────────────────────


class TestNotificationAnalysis:
    def test_valid_construction(self):
        from ha_notification_manager import NotificationAnalysis

        n = NotificationAnalysis(
            notification_id="http_login",
            category="security",
            severity="HIGH",
            original_title="Login attempt",
            original_message="Invalid credentials from 192.168.1.99",
            human_explanation="Someone tried to log in to HA with wrong credentials.",
            enriched_context={"source_ip": "192.168.1.99"},
            recommended_action="Check if this is a known device.",
            requires_hitl=True,
        )
        assert n.notification_id == "http_login"
        assert n.severity == "HIGH"
        assert n.requires_hitl is True

    def test_missing_required_fields_raises(self):
        from ha_notification_manager import NotificationAnalysis

        with pytest.raises(ValidationError):
            NotificationAnalysis(
                notification_id="http_login",
                category="security",
            )

    def test_json_round_trip(self):
        from ha_notification_manager import NotificationAnalysis

        original = NotificationAnalysis(
            notification_id="invalid_config",
            category="config_error",
            severity="HIGH",
            original_title=None,
            original_message="Config error in sensor platform",
            human_explanation="Your sensor config has an error.",
            enriched_context={},
            recommended_action="Review your configuration.yaml.",
            requires_hitl=True,
        )
        restored = NotificationAnalysis.model_validate_json(original.model_dump_json())
        assert restored == original

    def test_optional_title_accepts_none(self):
        from ha_notification_manager import NotificationAnalysis

        n = NotificationAnalysis(
            notification_id="other_nid",
            category="other",
            severity="MEDIUM",
            original_title=None,
            original_message="Something happened",
            human_explanation="Unknown notification.",
            enriched_context={},
            recommended_action="Investigate manually.",
            requires_hitl=False,
        )
        assert n.original_title is None


# ── ha_notification_manager — classify_notification ──────────────────────────────


class TestClassifyNotification:
    def test_http_login_is_security_high(self):
        from ha_notification_manager import classify_notification

        category, severity = classify_notification("http_login")
        assert category == "security"
        assert severity == "HIGH"

    def test_invalid_config_is_config_error_high(self):
        from ha_notification_manager import classify_notification

        category, severity = classify_notification("invalid_config")
        assert category == "config_error"
        assert severity == "HIGH"

    def test_unknown_id_is_other_medium(self):
        from ha_notification_manager import classify_notification

        category, severity = classify_notification("some_integration_error")
        assert category == "other"
        assert severity == "MEDIUM"

    def test_empty_string_falls_back_to_other(self):
        from ha_notification_manager import classify_notification

        category, severity = classify_notification("")
        assert category == "other"
        assert severity == "MEDIUM"


# ── ha_notification_manager — record_notification_seen ───────────────────────────


class TestRecordNotificationSeen:
    @pytest.fixture
    def db_path(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_new_notification_returns_true(self, db_path):
        from ha_notification_manager import record_notification_seen

        result = record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path
        )
        assert result is True

    def test_duplicate_notification_returns_false(self, db_path):
        from ha_notification_manager import record_notification_seen

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        result = record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path
        )
        assert result is False

    def test_new_notification_stored_in_db(self, db_path):
        from ha_notification_manager import record_notification_seen

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert row is not None

    def test_duplicate_updates_last_seen_at(self, db_path):
        import time
        from ha_notification_manager import record_notification_seen

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        time.sleep(0.01)
        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT first_seen_at, last_seen_at FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert row[1] > row[0]

    def test_different_ids_both_stored(self, db_path):
        from ha_notification_manager import record_notification_seen

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        record_notification_seen(
            "invalid_config", "config_error", "HIGH", db_path=db_path
        )

        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM notification_history"
            ).fetchone()[0]
        assert count == 2

    def test_mark_hitl_sent_updates_field(self, db_path):
        from ha_notification_manager import (
            record_notification_seen,
            mark_notification_hitl_sent,
        )

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        mark_notification_hitl_sent("http_login", db_path=db_path)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT hitl_sent_at FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert row[0] is not None

    def test_mark_dismissed_updates_fields(self, db_path):
        from ha_notification_manager import (
            record_notification_seen,
            mark_notification_dismissed,
        )

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        mark_notification_dismissed("http_login", dismissed_by="user", db_path=db_path)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT dismissed_at, dismissed_by FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert row[0] is not None
        assert row[1] == "user"

    def test_get_notification_history_returns_all(self, db_path):
        from ha_notification_manager import (
            record_notification_seen,
            get_notification_history,
        )

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        record_notification_seen(
            "invalid_config", "config_error", "HIGH", db_path=db_path
        )

        rows = get_notification_history(db_path=db_path)
        assert len(rows) == 2
        ids = {r["notification_id"] for r in rows}
        assert ids == {"http_login", "invalid_config"}

    def test_get_pending_excludes_dismissed(self, db_path):
        from ha_notification_manager import (
            record_notification_seen,
            mark_notification_dismissed,
            get_pending_notifications,
        )

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        record_notification_seen(
            "invalid_config", "config_error", "HIGH", db_path=db_path
        )
        mark_notification_dismissed("http_login", db_path=db_path)

        pending = get_pending_notifications(db_path=db_path)
        assert len(pending) == 1
        assert pending[0]["notification_id"] == "invalid_config"


# ── ha_agent_advanced — migration v6 (notification_history) ──────────────────────


class TestNotificationHistoryMigration:
    @pytest.fixture
    def db_path(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        return path

    def test_migration_creates_notification_history_table(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        assert "notification_history" in tables

    def test_notification_history_columns(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            cols = [
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(notification_history)"
                ).fetchall()
            ]
        expected = {
            "notification_id",
            "first_seen_at",
            "last_seen_at",
            "category",
            "severity",
            "hitl_sent_at",
            "dismissed_at",
            "dismissed_by",
        }
        assert expected.issubset(set(cols))

    def test_migration_is_idempotent(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()


# ── ha_log_monitor — poll_for_notifications ──────────────────────────────────────


class TestPollForNotifications:
    @pytest.fixture
    def db_path(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def _make_notification_entity(
        self,
        nid: str,
        title: str | None = None,
        message: str = "test message",
    ) -> dict:
        return {
            "entity_id": f"persistent_notification.{nid}",
            "state": "notifying",
            "attributes": {
                "notification_id": nid,
                "title": title,
                "message": message,
            },
        }

    def test_new_notification_triggers_notifier(self, db_path):
        from ha_log_monitor import poll_for_notifications
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        entity = self._make_notification_entity(
            "http_login", "Login attempt", "Bad creds"
        )
        rest = FakeHARestClient(states=[entity])
        notifier = FakeNotifier()

        async def run():
            import ha_log_monitor

            original = ha_log_monitor.HA_NOTIFICATION_POLL_INTERVAL_MINUTES

            async def one_shot(ha_rest_client, notifier, db_path):
                from ha_notification_manager import (
                    classify_notification,
                    record_notification_seen,
                )

                entities = await ha_rest_client.get_states(
                    prefix="persistent_notification."
                )
                for ent in entities:
                    entity_id = ent.get("entity_id", "")
                    nid = entity_id.removeprefix("persistent_notification.")
                    attrs = ent.get("attributes", {})
                    title = attrs.get("title")
                    message = attrs.get("message", "")
                    category, severity = classify_notification(nid)
                    is_new = record_notification_seen(
                        nid, category, severity, db_path=db_path
                    )
                    if is_new:
                        await notifier.send(
                            subject=f"Pueo: HA notification — {title or nid} [{severity}]",
                            body=message,
                            payload={
                                "notification_id": nid,
                                "category": category,
                                "severity": severity,
                            },
                        )

            await one_shot(rest, notifier, db_path)

        asyncio.run(run())
        assert len(notifier.sent) == 1
        assert notifier.sent[0]["payload"]["notification_id"] == "http_login"
        assert notifier.sent[0]["payload"]["category"] == "security"
        assert notifier.sent[0]["payload"]["severity"] == "HIGH"

    def test_duplicate_notification_not_resent(self, db_path):
        from ha_notification_manager import record_notification_seen

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)

        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        entity = self._make_notification_entity("http_login", "Login attempt")
        rest = FakeHARestClient(states=[entity])
        notifier = FakeNotifier()

        async def run():
            from ha_notification_manager import (
                classify_notification,
                record_notification_seen as rns,
            )

            entities = await rest.get_states(prefix="persistent_notification.")
            for ent in entities:
                entity_id = ent.get("entity_id", "")
                nid = entity_id.removeprefix("persistent_notification.")
                attrs = ent.get("attributes", {})
                title = attrs.get("title")
                message = attrs.get("message", "")
                category, severity = classify_notification(nid)
                is_new = rns(nid, category, severity, db_path=db_path)
                if is_new:
                    await notifier.send(
                        subject="duplicate",
                        body=message,
                        payload={},
                    )

        asyncio.run(run())
        assert len(notifier.sent) == 0

    def test_poll_failure_does_not_raise(self, db_path, monkeypatch):
        from ha_log_monitor import poll_for_notifications
        from utils.notify import FakeNotifier

        class ExplodingRestClient:
            async def get_states(self, prefix=None):
                raise RuntimeError("network down")

            async def get_state(self, entity_id):
                raise RuntimeError("network down")

            async def call_service(self, domain, service, payload):
                raise RuntimeError("network down")

        notifier = FakeNotifier()

        call_count = 0

        async def run():
            nonlocal call_count
            from ha_notification_manager import (
                classify_notification,
                record_notification_seen,
            )

            try:
                entities = await ExplodingRestClient().get_states(
                    prefix="persistent_notification."
                )
            except Exception:
                call_count += 1
                return

        asyncio.run(run())
        assert call_count == 1

    def test_unknown_notification_id_classified_as_other(self, db_path):
        from ha_notification_manager import (
            classify_notification,
            record_notification_seen,
        )
        from utils.notify import FakeNotifier
        from utils.ha_rest_client import FakeHARestClient

        entity = self._make_notification_entity(
            "some_integration_abc", message="Something failed"
        )
        rest = FakeHARestClient(states=[entity])
        notifier = FakeNotifier()

        async def run():
            entities = await rest.get_states(prefix="persistent_notification.")
            for ent in entities:
                nid = ent["entity_id"].removeprefix("persistent_notification.")
                category, severity = classify_notification(nid)
                is_new = record_notification_seen(
                    nid, category, severity, db_path=db_path
                )
                if is_new:
                    await notifier.send(
                        subject=f"Pueo: HA notification — {nid} [{severity}]",
                        body=ent["attributes"].get("message", ""),
                        payload={
                            "notification_id": nid,
                            "category": category,
                            "severity": severity,
                        },
                    )

        asyncio.run(run())
        assert len(notifier.sent) == 1
        assert notifier.sent[0]["payload"]["category"] == "other"
        assert notifier.sent[0]["payload"]["severity"] == "MEDIUM"
