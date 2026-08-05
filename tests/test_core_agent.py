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

    def test_main_schedules_notification_polling_when_configured(self, monkeypatch):
        import asyncio as asyncio_mod
        import ha_log_monitor
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        created_names: list[str] = []

        async def fake_tail(*args, **kwargs):
            pass

        def fake_create_task(coro: object, **kwargs: object) -> object:
            name = getattr(coro, "__qualname__", type(coro).__qualname__)
            created_names.append(name)
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]
            loop = asyncio_mod.get_running_loop()
            f: asyncio.Future[None] = loop.create_future()
            f.set_result(None)
            return f

        monkeypatch.setattr(ha_log_monitor, "tail_remote_log_stream", fake_tail)
        monkeypatch.setattr(asyncio_mod, "create_task", fake_create_task)
        monkeypatch.setattr(
            ha_log_monitor, "HA_NOTIFICATION_POLL_INTERVAL_MINUTES", 5.0
        )
        monkeypatch.setattr(ha_log_monitor, "HA_API_TOKEN", "fake-token")
        monkeypatch.setattr(ha_log_monitor, "HA_UPDATE_CHECK_INTERVAL_HOURS", 0.0)

        asyncio.run(
            ha_log_monitor.main(
                ha_rest_client=FakeHARestClient(),
                notifier=FakeNotifier(),
            )
        )
        assert any("poll_for_notifications" in n for n in created_names)

    def test_main_skips_notification_polling_when_interval_zero(self, monkeypatch):
        import asyncio as asyncio_mod
        import ha_log_monitor
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        created_names: list[str] = []

        async def fake_tail(*args, **kwargs):
            pass

        def fake_create_task(coro: object, **kwargs: object) -> object:
            name = getattr(coro, "__qualname__", type(coro).__qualname__)
            created_names.append(name)
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]
            loop = asyncio_mod.get_running_loop()
            f: asyncio.Future[None] = loop.create_future()
            f.set_result(None)
            return f

        monkeypatch.setattr(ha_log_monitor, "tail_remote_log_stream", fake_tail)
        monkeypatch.setattr(asyncio_mod, "create_task", fake_create_task)
        monkeypatch.setattr(
            ha_log_monitor, "HA_NOTIFICATION_POLL_INTERVAL_MINUTES", 0.0
        )
        monkeypatch.setattr(ha_log_monitor, "HA_API_TOKEN", "fake-token")
        monkeypatch.setattr(ha_log_monitor, "HA_UPDATE_CHECK_INTERVAL_HOURS", 0.0)

        asyncio.run(
            ha_log_monitor.main(
                ha_rest_client=FakeHARestClient(),
                notifier=FakeNotifier(),
            )
        )
        assert not any("poll_for_notifications" in n for n in created_names)


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

    def test_schema_version_is_current_after_init(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 13

    def test_version_unchanged_on_second_init(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 13

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
        assert version == 13

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

    def test_record_backup_slug_stores_explicit_name(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("slug-named", name="Pueo_2026-08-05_1430")
        with sqlite3.connect(db_path) as conn:
            name = conn.execute(
                "SELECT name FROM backup_registry WHERE backup_slug = 'slug-named'"
            ).fetchone()[0]
        assert name == "Pueo_2026-08-05_1430"

    def test_record_backup_slug_auto_generates_name(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("slug-auto")
        with sqlite3.connect(db_path) as conn:
            name = conn.execute(
                "SELECT name FROM backup_registry WHERE backup_slug = 'slug-auto'"
            ).fetchone()[0]
        assert name is not None and name.startswith("Pueo_")

    def test_parse_backup_list_valid_json(self):
        import ha_agent_advanced

        output = '{"result":"ok","data":{"backups":[{"slug":"abc123","size_bytes":56760320,"name":"Pueo_2026-08-05_1430"}]}}'
        result = ha_agent_advanced._parse_backup_list(output)
        assert result == [
            {"slug": "abc123", "size_bytes": 56760320, "name": "Pueo_2026-08-05_1430"}
        ]

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
        assert result == [{"slug": "xyz", "size_bytes": 0, "name": ""}]

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

    def test_reconcile_backfills_size_and_name_for_zero_byte_rows(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug(
            "zero-slug"
        )  # inserts size_bytes=0, name=auto
        # Override name to NULL to simulate pre-v11 rows
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE backup_registry SET size_bytes = 0, name = NULL WHERE backup_slug = 'zero-slug'"
            )
            conn.commit()
        backup_json = '{"result":"ok","data":{"backups":[{"slug":"zero-slug","size_bytes":54321,"name":"HA Auto Backup"}]}}'
        ssh = FakeSSHClient(command_results={"ha backups list": (0, backup_json, "")})
        asyncio.run(ha_agent_advanced.reconcile_backup_inventory(ssh_client=ssh))
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT size_bytes, name FROM backup_registry WHERE backup_slug = 'zero-slug'"
            ).fetchone()
        assert row[0] == 54321
        assert row[1] == "HA Auto Backup"

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

    def test_reconcile_skips_deleted_from_ha_in_orphan_check(self, db_path, caplog):
        import logging

        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.record_backup_slug("already-gone")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE backup_registry SET deleted_from_ha_at = ? WHERE backup_slug = ?",
                (1234567890.0, "already-gone"),
            )
            conn.commit()
        backup_json = '{"result":"ok","data":{"backups":[]}}'
        ssh = FakeSSHClient(command_results={"ha backups list": (0, backup_json, "")})
        with caplog.at_level(logging.WARNING):
            asyncio.run(ha_agent_advanced.reconcile_backup_inventory(ssh_client=ssh))
        assert not any("orphan" in r.message for r in caplog.records)


# ── Backup offloading ─────────────────────────────────────────────────────────────


class TestBackupOffloading:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    @pytest.fixture(autouse=True)
    def _patch_resolve_direct(self, monkeypatch):
        """Make _resolve_backup_remote_path return the direct {slug}.tar path by default."""
        import ha_agent_advanced

        async def _direct(slug, client):
            return f"/backup/{slug}.tar"

        monkeypatch.setattr(ha_agent_advanced, "_resolve_backup_remote_path", _direct)

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
                "SELECT location, size_bytes FROM backup_registry WHERE backup_slug = ?",
                (slug,),
            ).fetchone()
        assert row[0] == "both"
        assert row[1] == len(content)

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

    def test_offload_uses_fallback_path_for_descriptive_filename(
        self, db_path, monkeypatch, tmp_path
    ):
        import asyncio
        import hashlib
        import sqlite3
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        slug = "abc123"
        self._insert_slug(db_path, slug)
        descriptive_path = "/backup/Automatic_backup_2026.7.2_2026-08-03.tar"
        content = b"ha-auto-backup content"
        remote_hash = hashlib.sha256(content).hexdigest()
        local_dir = tmp_path / "backups"
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_LOCAL_DIR", str(local_dir))

        async def _fallback_resolve(s, client):
            return descriptive_path if s == slug else None

        monkeypatch.setattr(
            ha_agent_advanced, "_resolve_backup_remote_path", _fallback_resolve
        )
        ssh = FakeSSHClient(
            download_contents={descriptive_path: content},
            command_results={
                "sha256sum": (0, f"{remote_hash}  {descriptive_path}\n", "")
            },
        )
        asyncio.run(ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh))

        assert ssh.downloaded_files[0][0] == descriptive_path
        assert (local_dir / f"{slug}.tar").exists()
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = ?", (slug,)
            ).fetchone()
        assert row[0] == "both"

    def test_offload_returns_false_when_no_remote_path(
        self, db_path, monkeypatch, tmp_path
    ):
        import asyncio
        import sqlite3
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        slug = "abc123"
        self._insert_slug(db_path, slug)
        monkeypatch.setattr(
            ha_agent_advanced, "BACKUP_LOCAL_DIR", str(tmp_path / "backups")
        )

        async def _not_found(s, client):
            return None

        monkeypatch.setattr(
            ha_agent_advanced, "_resolve_backup_remote_path", _not_found
        )
        ssh = FakeSSHClient()
        result = asyncio.run(
            ha_agent_advanced.offload_backup_to_local(slug, ssh_client=ssh)
        )

        assert result is False
        assert ssh.downloaded_files == []
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = ?", (slug,)
            ).fetchone()
        assert row[0] == "ha"


# ── _resolve_backup_remote_path ───────────────────────────────────────────────────


class TestResolveBackupRemotePath:
    def test_direct_path_returned_when_slug_tar_exists(self):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        slug = "abc123"
        ssh = FakeSSHClient(
            command_results={f"[ -f /backup/{slug}.tar ]": (0, "found", "")}
        )
        result = asyncio.run(ha_agent_advanced._resolve_backup_remote_path(slug, ssh))
        assert result == f"/backup/{slug}.tar"

    def test_fallback_ssh_search_when_direct_not_found(self):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        slug = "abc123"
        descriptive = "/backup/Automatic_backup_2026.7.2.tar"
        ssh = FakeSSHClient(
            command_results={
                f"[ -f /backup/{slug}.tar ]": (0, "", ""),
                "for f in": (0, descriptive, ""),
            }
        )
        result = asyncio.run(ha_agent_advanced._resolve_backup_remote_path(slug, ssh))
        assert result == descriptive

    def test_returns_none_when_not_found_anywhere(self):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        result = asyncio.run(
            ha_agent_advanced._resolve_backup_remote_path("abc123", ssh)
        )
        assert result is None

    def test_returns_none_for_non_hex_slug(self):
        import asyncio
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        result = asyncio.run(
            ha_agent_advanced._resolve_backup_remote_path("slug-abc", ssh)
        )
        assert result is None
        assert ssh.commands_run == []


# ── ha_agent_sandbox_engine SQLite layer ─────────────────────────────────────────


class TestSandboxDB:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced
        import ha_agent_sandbox_engine

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_sandbox_engine, "DB_PATH", path)
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
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

    def test_schema_version_is_current_after_init(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 13

    def test_version_unchanged_on_second_init(self, db_path):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        ha_agent_sandbox_engine.init_local_database()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 13

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
        assert version == 13

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


class TestSupervisorMain:
    """Tests for supervisor_main() — verifies loop selection based on config."""

    def _make_config(
        self,
        tmp_path,
        *,
        api_token: str = "",
        update_interval_h: float = 0.0,
        notif_interval_m: float = 0.0,
        netalertx_host: str = "",
    ) -> Path:
        cfg = {
            "home_assistant": {"host": "ha.local", "api_token": api_token},
            "agent": {
                "update_check_interval_hours": update_interval_h,
                "notification_poll_interval_minutes": notif_interval_m,
                "notify_watch_dir": str(tmp_path / "hitl"),
            },
            "netalertx": {"host": netalertx_host},
        }
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump(cfg))
        (tmp_path / "hitl").mkdir(exist_ok=True)
        return p

    def _patch_all(self, monkeypatch, config_path: Path) -> list:
        """Patch external deps so supervisor_main() runs and terminates immediately."""
        import config as cfg_mod
        import ha_agent_advanced
        import ha_log_monitor
        import uvicorn
        import utils.resource as rm
        import utils.ssh_client as sc
        import netalertx.log_monitor as nax
        from utils.notify import FakeNotifier

        monkeypatch.setenv("PUEO_CONFIG", str(config_path))
        importlib.reload(cfg_mod)

        monkeypatch.setattr(ha_agent_advanced, "init_local_database", lambda: None)

        async def _noop(**kw):
            pass

        monkeypatch.setattr(
            ha_log_monitor, "tail_remote_log_stream", lambda **kw: _noop()
        )
        monkeypatch.setattr(ha_log_monitor, "poll_for_updates", lambda **kw: _noop())
        monkeypatch.setattr(
            ha_log_monitor, "poll_for_notifications", lambda **kw: _noop()
        )
        monkeypatch.setattr(nax, "tail_netalertx_log_stream", lambda **kw: _noop())

        class _FakePoller:
            def __init__(self, *a, **kw):
                pass

            async def run(self):
                pass

        monkeypatch.setattr(rm, "ResourcePoller", _FakePoller)
        monkeypatch.setattr(sc, "AsyncSSHClient", lambda *a, **kw: object())

        import utils.notify as notify_mod

        monkeypatch.setattr(notify_mod, "get_notifier", lambda *a, **kw: FakeNotifier())

        # Uvicorn — returns immediately so supervisor_main() can complete
        class _FakeServer:
            async def serve(self):
                pass

        monkeypatch.setattr(uvicorn, "Config", lambda *a, **kw: object())
        monkeypatch.setattr(uvicorn, "Server", lambda c: _FakeServer())

        # Track which loop names were started
        started: list = []
        from utils.supervisor import LoopSupervisor

        orig_start = LoopSupervisor.start

        def _tracking_start(self, name, factory):
            started.append(name)
            orig_start(self, name, factory)

        monkeypatch.setattr(LoopSupervisor, "start", _tracking_start)
        return started

    def test_core_loops_always_start(self, monkeypatch, tmp_path):
        """ha_log_monitor and resource_poll start regardless of optional config."""
        config_path = self._make_config(tmp_path)
        started = self._patch_all(monkeypatch, config_path)

        import main as m

        asyncio.run(m.supervisor_main(config_path))

        assert "ha_log_monitor" in started
        assert "resource_poll" in started

    def test_update_check_loop_disabled_without_token(self, monkeypatch, tmp_path):
        """update_check loop doesn't start when api_token is empty."""
        config_path = self._make_config(tmp_path, update_interval_h=1.0, api_token="")
        started = self._patch_all(monkeypatch, config_path)

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "update_check" not in started

    def test_update_check_loop_starts_with_token_and_interval(
        self, monkeypatch, tmp_path
    ):
        """update_check loop starts when api_token and interval are both set."""
        config_path = self._make_config(
            tmp_path, update_interval_h=1.0, api_token="test-tok"
        )
        started = self._patch_all(monkeypatch, config_path)

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "update_check" in started

    def test_notification_poll_loop_disabled_without_token(self, monkeypatch, tmp_path):
        """notification_poll loop doesn't start when api_token is empty."""
        config_path = self._make_config(tmp_path, notif_interval_m=5.0, api_token="")
        started = self._patch_all(monkeypatch, config_path)

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "notification_poll" not in started

    def test_notification_poll_loop_starts_with_token_and_interval(
        self, monkeypatch, tmp_path
    ):
        """notification_poll starts when token and interval are set."""
        config_path = self._make_config(
            tmp_path, notif_interval_m=5.0, api_token="test-tok"
        )
        started = self._patch_all(monkeypatch, config_path)

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "notification_poll" in started

    def test_netalertx_loop_disabled_when_host_empty(self, monkeypatch, tmp_path):
        """netalertx loop doesn't start when NETALERTX_HOST resolves to empty string."""
        config_path = self._make_config(tmp_path, netalertx_host="")
        started = self._patch_all(monkeypatch, config_path)

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "netalertx" not in started

    def test_netalertx_loop_starts_when_host_set(self, monkeypatch, tmp_path):
        """netalertx loop starts when netalertx.host is configured."""
        config_path = self._make_config(tmp_path, netalertx_host="ha.local")
        started = self._patch_all(monkeypatch, config_path)

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "netalertx" in started

    def test_netalertx_setup_loop_registered_when_desired_and_not_operational(
        self, monkeypatch, tmp_path
    ):
        """netalertx_setup loop starts when setup_desired=true and not yet FULLY_OPERATIONAL."""
        cfg_data = {
            "home_assistant": {"host": "ha.local"},
            "netalertx": {"setup_desired": True},
            "agent": {"notify_watch_dir": str(tmp_path / "hitl")},
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(cfg_data))
        (tmp_path / "hitl").mkdir(exist_ok=True)
        started = self._patch_all(monkeypatch, config_path)

        import netalertx.installer as nax_inst

        monkeypatch.setattr(nax_inst, "get_install_state", lambda db: "ADDON_RUNNING")

        async def _noop(**kw):
            pass

        monkeypatch.setattr(nax_inst, "main", _noop)

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "netalertx_setup" in started

    def test_netalertx_setup_loop_not_registered_when_fully_operational(
        self, monkeypatch, tmp_path
    ):
        """netalertx_setup loop is skipped when install state is FULLY_OPERATIONAL."""
        cfg_data = {
            "home_assistant": {"host": "ha.local"},
            "netalertx": {"setup_desired": True},
            "agent": {"notify_watch_dir": str(tmp_path / "hitl")},
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(cfg_data))
        (tmp_path / "hitl").mkdir(exist_ok=True)
        started = self._patch_all(monkeypatch, config_path)

        import netalertx.installer as nax_inst

        monkeypatch.setattr(
            nax_inst, "get_install_state", lambda db: "FULLY_OPERATIONAL"
        )

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "netalertx_setup" not in started

    def test_netalertx_setup_loop_not_registered_when_not_desired(
        self, monkeypatch, tmp_path
    ):
        """netalertx_setup loop is skipped when setup_desired is false (the default)."""
        config_path = self._make_config(tmp_path)
        started = self._patch_all(monkeypatch, config_path)

        import main as m

        asyncio.run(m.supervisor_main(config_path))
        assert "netalertx_setup" not in started


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
        from utils.ollama_client import FakeToolCallingLLMClient

        return FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "Config is valid",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                }
            ]
        )

    @pytest.fixture
    def llm_with_fix(self):
        from utils.ollama_client import FakeToolCallingLLMClient

        return FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "apply_fix",
                                "arguments": {
                                    "yaml_content": _FIXED_CONFIG,
                                    "description": "Fix port number",
                                },
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "Fixed the port",
                                    "action_taken": "fixed",
                                },
                            }
                        }
                    ]
                },
            ]
        )

    @pytest.fixture
    def llm_bad_fix(self):
        from utils.ollama_client import FakeToolCallingLLMClient

        return FakeToolCallingLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "apply_fix",
                                "arguments": {
                                    "yaml_content": _BAD_FIX,
                                    "description": "Fix something",
                                },
                            }
                        }
                    ]
                },
            ]
        )

    @pytest.fixture
    def gate_auto(self):
        from utils.autonomy import FakeAutonomyGate

        return FakeAutonomyGate(auto_execute_result=True)

    @pytest.fixture
    def knowledge_store(self):
        from utils.knowledge_store import FakeKnowledgeStore

        return FakeKnowledgeStore()

    def test_valid_config_no_backup_taken(
        self, ssh_ok, llm_valid, db_path, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_valid, knowledge_store=knowledge_store
            )
        )
        assert not any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_valid_config_records_state(
        self, ssh_ok, llm_valid, db_path, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok, llm_client=llm_valid, knowledge_store=knowledge_store
            )
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM state_history").fetchone()[0]
        assert count == 1

    def test_repair_path_writes_config(
        self, ssh_ok, llm_with_fix, db_path, gate_auto, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok,
                llm_client=llm_with_fix,
                gate=gate_auto,
                knowledge_store=knowledge_store,
            )
        )
        assert "/config/configuration.yaml" in ssh_ok.written_files
        assert ssh_ok.written_files["/config/configuration.yaml"] == _FIXED_CONFIG

    def test_repair_path_backup_recorded(
        self, ssh_ok, llm_with_fix, db_path, gate_auto, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok,
                llm_client=llm_with_fix,
                gate=gate_auto,
                knowledge_store=knowledge_store,
            )
        )
        with sqlite3.connect(db_path) as conn:
            slug = conn.execute("SELECT backup_slug FROM backup_registry").fetchone()
        assert slug is not None
        assert slug[0] == "sbx-slug-1"

    def test_sandbox_fail_aborts_atomic_swap(
        self, ssh_sandbox_fail, llm_with_fix, db_path, gate_auto, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_sandbox_fail,
                llm_client=llm_with_fix,
                gate=gate_auto,
                knowledge_store=knowledge_store,
            )
        )
        assert "/config/configuration.yaml" not in ssh_sandbox_fail.written_files

    def test_bad_fix_rejected_before_backup(
        self, ssh_ok, llm_bad_fix, db_path, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok,
                llm_client=llm_bad_fix,
                knowledge_store=knowledge_store,
            )
        )
        assert not any("ha backup new" in cmd for cmd in ssh_ok.commands_run)

    def test_bad_fix_records_rejection_in_state(
        self, ssh_ok, llm_bad_fix, db_path, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok,
                llm_client=llm_bad_fix,
                knowledge_store=knowledge_store,
            )
        )
        with sqlite3.connect(db_path) as conn:
            action = conn.execute("SELECT action_taken FROM state_history").fetchone()
        assert action is not None
        # AgentLoop records fix_failed outcome when apply_fix validation fails
        assert "fix" in action[0].lower()

    def test_sandbox_fail_records_state(
        self, ssh_sandbox_fail, llm_with_fix, db_path, gate_auto, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_sandbox_fail,
                llm_client=llm_with_fix,
                gate=gate_auto,
                knowledge_store=knowledge_store,
            )
        )
        with sqlite3.connect(db_path) as conn:
            action = conn.execute("SELECT action_taken FROM state_history").fetchone()
        assert action is not None
        # AgentLoop records fix_failed outcome when sandbox test fails
        assert "fix" in action[0].lower()

    def test_repair_path_llm_consulted(
        self, ssh_ok, llm_with_fix, db_path, gate_auto, knowledge_store
    ):
        import ha_agent_sandbox_engine

        ha_agent_sandbox_engine.init_local_database()
        asyncio.run(
            ha_agent_sandbox_engine.main(
                ssh_client=ssh_ok,
                llm_client=llm_with_fix,
                gate=gate_auto,
                knowledge_store=knowledge_store,
            )
        )
        # AgentLoop makes one chat_with_tools call per tool-calling step
        assert len(llm_with_fix.calls) >= 1


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

    def test_os_component_mapped_for_modern_entity_name(self):
        # HAOS entity is update.home_assistant_operating_system_update on modern HA,
        # not update.home_assistant_os_update. Without this map entry the component
        # falls through to the raw string and execute_update routes it to the add-on
        # executor instead of execute_os_update.
        from utils.ha_rest_client import _entity_to_update_status

        entity = {
            "entity_id": "update.home_assistant_operating_system_update",
            "state": "on",
            "attributes": {
                "installed_version": "18.1",
                "latest_version": "18.2",
            },
        }
        status = _entity_to_update_status(entity)
        assert status.component == "os"
        assert status.update_available is True


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
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

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
        updates = asyncio.run(
            run_update_check(
                ha_rest_client=fake,
                gate=FakeAutonomyGate(auto_execute_result=False, approval_result=False),
                notifier=FakeNotifier(approve=False),
            )
        )
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
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

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
                gate=FakeAutonomyGate(auto_execute_result=False, approval_result=False),
                notifier=FakeNotifier(approve=False),
            )
        )
        assert len(updates) == 1

    def test_analyze_breaking_changes_error_is_skipped(self, tmp_path, capsys):
        """analyze_breaking_changes failure prints a warning and returns all updates."""
        import unittest.mock as mock
        from ha_update_manager import run_update_check
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

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
                    gate=FakeAutonomyGate(
                        auto_execute_result=False, approval_result=False
                    ),
                    notifier=FakeNotifier(approve=False),
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
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
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
                gate=FakeAutonomyGate(auto_execute_result=False, approval_result=False),
                notifier=FakeNotifier(approve=False),
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
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
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
                    gate=FakeAutonomyGate(
                        auto_execute_result=False, approval_result=False
                    ),
                    notifier=FakeNotifier(approve=False),
                )
            )
        assert llm.calls == []
        out = capsys.readouterr().out
        assert "Warning" in out

    def test_hitl_card_sent_for_core_update(self, tmp_path):
        """A HITL approval card is dispatched when a Core update is detected."""
        from ha_update_manager import run_update_check
        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)

        asyncio.run(
            run_update_check(
                ha_rest_client=self._fake_rest(),
                cache_dir=str(tmp_path),
                gate=gate,
                notifier=notifier,
            )
        )

        assert len(gate.require_approval_calls) == 1
        assert gate.require_approval_calls[0]["risk"] == RiskLevel.CRITICAL

    def test_hitl_card_sent_on_approval_but_execute_not_called(self, tmp_path):
        """run_update_check sends the HITL card but never calls execute_update.

        Execution is the dashboard's responsibility via _execute_queued_update.
        Keeping execute_update out of run_update_check prevents double-execution
        when both the dashboard and the one-shot subprocess see the approval sentinel.
        """
        import unittest.mock as mock
        from ha_update_manager import run_update_check
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)

        with mock.patch(
            "ha_update_manager.execute_update",
            new=mock.AsyncMock(return_value=True),
        ) as mock_exec:
            asyncio.run(
                run_update_check(
                    ha_rest_client=self._fake_rest(),
                    cache_dir=str(tmp_path),
                    gate=gate,
                    notifier=notifier,
                )
            )

        assert mock_exec.call_count == 0
        assert len(gate.require_approval_calls) == 1

    def test_hitl_rejection_skips_execute_update(self, tmp_path):
        """When the HITL card is rejected, execute_update is not called."""
        import unittest.mock as mock
        from ha_update_manager import run_update_check
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=False)
        notifier = FakeNotifier(approve=False)

        with mock.patch(
            "ha_update_manager.execute_update",
            new=mock.AsyncMock(return_value=True),
        ) as mock_exec:
            asyncio.run(
                run_update_check(
                    ha_rest_client=self._fake_rest(),
                    cache_dir=str(tmp_path),
                    gate=gate,
                    notifier=notifier,
                )
            )

        assert mock_exec.call_count == 0


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


# ── _wait_for_ha_down ──────────────────────────────────────────────────────────
class TestWaitForHaDown:
    def test_returns_true_immediately_on_connection_refused(self):
        from ha_update_manager import _wait_for_ha_down

        async def refused_connect(host, port):
            raise ConnectionRefusedError("no server")

        result = asyncio.run(
            _wait_for_ha_down(
                "ha.local",
                8123,
                timeout_seconds=30,
                _sleep=_noop_sleep,
                _connect=refused_connect,
            )
        )
        assert result is True

    def test_returns_false_on_timeout_when_tcp_always_succeeds(self):
        from ha_update_manager import _wait_for_ha_down

        async def always_up(host, port):
            class FakeWriter:
                def close(self):
                    pass

                async def wait_closed(self):
                    pass

            return object(), FakeWriter()

        result = asyncio.run(
            _wait_for_ha_down(
                "ha.local",
                8123,
                timeout_seconds=0,
                _sleep=_noop_sleep,
                _connect=always_up,
            )
        )
        assert result is False

    def test_returns_true_after_several_successes_then_refused(self):
        from ha_update_manager import _wait_for_ha_down

        calls = [0]

        async def flaky(host, port):
            calls[0] += 1
            if calls[0] >= 3:
                raise OSError("host down")

            class FakeWriter:
                def close(self):
                    pass

                async def wait_closed(self):
                    pass

            return object(), FakeWriter()

        result = asyncio.run(
            _wait_for_ha_down(
                "ha.local",
                8123,
                timeout_seconds=60,
                _sleep=_noop_sleep,
                _connect=flaky,
            )
        )
        assert result is True
        assert calls[0] == 3


# ── _poll_ha_api_ready ─────────────────────────────────────────────────────────
class TestPollHaApiReady:
    def test_returns_true_on_http_200(self):
        from ha_update_manager import _poll_ha_api_ready

        async def ok_get(url, headers):
            return 200

        result = asyncio.run(
            _poll_ha_api_ready(
                "ha.local",
                8123,
                "token",
                timeout_seconds=30,
                _sleep=_noop_sleep,
                _get=ok_get,
            )
        )
        assert result is True

    def test_returns_true_on_http_401(self):
        from ha_update_manager import _poll_ha_api_ready

        async def unauth_get(url, headers):
            return 401

        result = asyncio.run(
            _poll_ha_api_ready(
                "ha.local",
                8123,
                "token",
                timeout_seconds=30,
                _sleep=_noop_sleep,
                _get=unauth_get,
            )
        )
        assert result is True

    def test_returns_false_on_timeout(self):
        from ha_update_manager import _poll_ha_api_ready

        async def error_get(url, headers):
            raise ConnectionRefusedError("down")

        result = asyncio.run(
            _poll_ha_api_ready(
                "ha.local",
                8123,
                "token",
                timeout_seconds=0,
                _sleep=_noop_sleep,
                _get=error_get,
            )
        )
        assert result is False

    def test_retries_on_connection_error_then_succeeds(self):
        from ha_update_manager import _poll_ha_api_ready

        calls = [0]

        async def flaky_get(url, headers):
            calls[0] += 1
            if calls[0] < 3:
                raise OSError("boot in progress")
            return 200

        result = asyncio.run(
            _poll_ha_api_ready(
                "ha.local",
                8123,
                "token",
                timeout_seconds=60,
                interval=1,
                _sleep=_noop_sleep,
                _get=flaky_get,
            )
        )
        assert result is True
        assert calls[0] == 3


# ── execute_ha_reboot ─────────────────────────────────────────────────────────
class TestExecuteHaReboot:
    def _make_ssh(self):
        from utils.ssh_client import FakeSSHClient

        return FakeSSHClient()

    def test_sends_success_card_after_api_ready(self, tmp_path, monkeypatch):
        from ha_update_manager import execute_ha_reboot
        from utils.notify import FakeNotifier

        monkeypatch.setenv("PUEO_CONFIG", str(tmp_path / "config.yaml"))

        ssh = self._make_ssh()
        notifier = FakeNotifier()
        api_calls = [0]

        async def fake_poll(host, port, timeout_seconds):
            return True

        async def fake_api(host, port, token, timeout_seconds):
            api_calls[0] += 1
            return True

        async def fake_down(host, port, timeout_seconds):
            return True

        with (
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.execute_remote_backup",
                return_value="abc123",
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.record_backup_slug"
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.offload_backup_to_local"
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch("asyncio.sleep"),
        ):
            result = asyncio.run(
                execute_ha_reboot(
                    ssh,
                    notifier,
                    ha_host="ha.local",
                    ha_port=8123,
                    _poll=fake_poll,
                    _api_poll=fake_api,
                    _down_poll=fake_down,
                )
            )

        assert result is True
        assert api_calls[0] == 1
        assert len(notifier.sent) == 1
        assert "succeeded" in notifier.sent[0]["subject"].lower()

    def test_sends_failure_card_when_api_poll_times_out(self, tmp_path, monkeypatch):
        from ha_update_manager import execute_ha_reboot
        from utils.notify import FakeNotifier

        monkeypatch.setenv("PUEO_CONFIG", str(tmp_path / "config.yaml"))

        ssh = self._make_ssh()
        notifier = FakeNotifier()

        async def fake_poll(host, port, timeout_seconds):
            return True

        async def fake_api(host, port, token, timeout_seconds):
            return False  # API never comes up

        async def fake_down(host, port, timeout_seconds):
            return True

        with (
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.execute_remote_backup",
                return_value="abc123",
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.record_backup_slug"
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.offload_backup_to_local"
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch("asyncio.sleep"),
        ):
            result = asyncio.run(
                execute_ha_reboot(
                    ssh,
                    notifier,
                    ha_host="ha.local",
                    ha_port=8123,
                    _poll=fake_poll,
                    _api_poll=fake_api,
                    _down_poll=fake_down,
                )
            )

        assert result is False
        assert len(notifier.sent) == 1
        assert "timed out" in notifier.sent[0]["subject"].lower()

    def test_skips_api_poll_when_tcp_poll_fails(self, tmp_path, monkeypatch):
        """If TCP poll times out, _api_poll must not be called."""
        from ha_update_manager import execute_ha_reboot
        from utils.notify import FakeNotifier

        monkeypatch.setenv("PUEO_CONFIG", str(tmp_path / "config.yaml"))

        ssh = self._make_ssh()
        notifier = FakeNotifier()
        api_calls = [0]

        async def fake_poll(host, port, timeout_seconds):
            return False

        async def fake_api(host, port, token, timeout_seconds):
            api_calls[0] += 1
            return True

        async def fake_down(host, port, timeout_seconds):
            return True

        with (
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.execute_remote_backup",
                return_value="abc123",
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.record_backup_slug"
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "ha_agent_advanced.offload_backup_to_local"
            ),
            __import__("unittest.mock", fromlist=["patch"]).patch("asyncio.sleep"),
        ):
            result = asyncio.run(
                execute_ha_reboot(
                    ssh,
                    notifier,
                    ha_host="ha.local",
                    ha_port=8123,
                    _poll=fake_poll,
                    _api_poll=fake_api,
                    _down_poll=fake_down,
                )
            )

        assert result is False
        assert api_calls[0] == 0


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


# ── _poll_addon_update_via_rest ───────────────────────────────────────────────
class TestPollAddonUpdateViaRest:
    def _make_rest_client(self, states: list[dict]):
        """Returns a fake REST client that cycles through the given entity states."""
        from utils.ha_rest_client import FakeHARestClient

        queue = list(states)

        class _QueuedClient(FakeHARestClient):
            async def get_state(self, entity_id: str) -> dict:  # type: ignore[override]
                if queue:
                    return queue.pop(0)
                return {"state": "on", "attributes": {"in_progress": False}}

        return _QueuedClient()

    def test_returns_true_when_state_flips_off(self):
        from ha_update_manager import _poll_addon_update_via_rest

        client = self._make_rest_client(
            [
                {"state": "on", "attributes": {"in_progress": True}},
                {"state": "off", "attributes": {"in_progress": False}},
            ]
        )
        result = asyncio.run(
            _poll_addon_update_via_rest(
                "update.file_editor",
                client,
                timeout_seconds=60,
                interval=5,
                _sleep=_noop_sleep,
            )
        )
        assert result is True

    def test_returns_false_on_timeout(self):
        from ha_update_manager import _poll_addon_update_via_rest

        client = self._make_rest_client([])
        result = asyncio.run(
            _poll_addon_update_via_rest(
                "update.file_editor",
                client,
                timeout_seconds=0,
                interval=5,
                _sleep=_noop_sleep,
            )
        )
        assert result is False

    def test_tolerates_get_state_exception(self):
        from ha_update_manager import _poll_addon_update_via_rest
        from utils.ha_rest_client import FakeHARestClient

        call_count = [0]

        class _ErrThenSuccessClient(FakeHARestClient):
            async def get_state(self, entity_id: str) -> dict:  # type: ignore[override]
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("transient error")
                return {"state": "off", "attributes": {"in_progress": False}}

        client = _ErrThenSuccessClient()
        result = asyncio.run(
            _poll_addon_update_via_rest(
                "update.file_editor",
                client,
                timeout_seconds=60,
                interval=5,
                _sleep=_noop_sleep,
            )
        )
        assert result is True


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

            def mock_record(s, name=""):
                recorded.append(s)

            async def mock_offload(slug, ssh_client=None):
                pass

            orig_b = ha_agent_advanced.execute_remote_backup
            orig_r = ha_agent_advanced.record_backup_slug
            orig_o = ha_agent_advanced.offload_backup_to_local
            ha_agent_advanced.execute_remote_backup = mock_backup
            ha_agent_advanced.record_backup_slug = mock_record
            ha_agent_advanced.offload_backup_to_local = mock_offload
            try:
                yield called, recorded
            finally:
                ha_agent_advanced.execute_remote_backup = orig_b
                ha_agent_advanced.record_backup_slug = orig_r
                ha_agent_advanced.offload_backup_to_local = orig_o

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

            async def mock_offload(slug, ssh_client=None):
                pass

            orig_b = ha_agent_advanced.execute_remote_backup
            orig_r = ha_agent_advanced.record_backup_slug
            orig_o = ha_agent_advanced.offload_backup_to_local
            ha_agent_advanced.execute_remote_backup = mock_backup
            ha_agent_advanced.record_backup_slug = lambda s, name="": None
            ha_agent_advanced.offload_backup_to_local = mock_offload
            try:
                yield
            finally:
                ha_agent_advanced.execute_remote_backup = orig_b
                ha_agent_advanced.record_backup_slug = orig_r
                ha_agent_advanced.offload_backup_to_local = orig_o

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

            async def mock_offload(slug, ssh_client=None):
                pass

            orig_b = ha_agent_advanced.execute_remote_backup
            orig_r = ha_agent_advanced.record_backup_slug
            orig_o = ha_agent_advanced.offload_backup_to_local
            ha_agent_advanced.execute_remote_backup = mock_backup
            ha_agent_advanced.record_backup_slug = lambda s, name="": None
            ha_agent_advanced.offload_backup_to_local = mock_offload
            try:
                yield
            finally:
                ha_agent_advanced.execute_remote_backup = orig_b
                ha_agent_advanced.record_backup_slug = orig_r
                ha_agent_advanced.offload_backup_to_local = orig_o

        return _ctx()

    def test_calls_backup_and_calls_rest_service(self):
        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        rest = FakeHARestClient()

        async def fake_poll(slug, version, client, timeout_seconds=180):
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_addon_update(
                    self._make_update("my_addon"),
                    ssh,
                    notifier,
                    gate,
                    _poll=fake_poll,
                    ha_rest_client=rest,
                )
            )

        assert result is True
        assert rest.service_calls == [
            ("update", "install", {"entity_id": "update.my_addon_update"})
        ]
        assert notifier.sent[0]["payload"]["success"] is True

    def test_returns_false_on_timeout(self):
        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        rest = FakeHARestClient()

        async def fake_poll_fail(slug, version, client, timeout_seconds=180):
            return False

        with self._patch_backup():
            result = asyncio.run(
                execute_addon_update(
                    self._make_update(),
                    ssh,
                    notifier,
                    gate,
                    _poll=fake_poll_fail,
                    ha_rest_client=rest,
                )
            )

        assert result is False
        assert notifier.sent[0]["payload"]["success"] is False

    def test_rest_service_uses_entity_id(self):
        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        rest = FakeHARestClient()

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
                    ha_rest_client=rest,
                )
            )

        assert len(rest.service_calls) == 1
        domain, service, payload = rest.service_calls[0]
        assert domain == "update"
        assert service == "install"
        assert "special_addon" in payload["entity_id"]

    def test_rest_failure_returns_false_immediately(self):
        """When call_service raises, return False without polling."""
        import httpx

        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        poll_called = []

        class _FailingRestClient(FakeHARestClient):
            async def call_service(self, domain, service, payload):
                raise httpx.HTTPStatusError(
                    "403 Forbidden",
                    request=httpx.Request(
                        "POST", "http://fake/api/services/update/install"
                    ),
                    response=httpx.Response(403),
                )

        async def fake_poll(slug, version, client, timeout_seconds=180):
            poll_called.append(slug)
            return False

        with self._patch_backup():
            result = asyncio.run(
                execute_addon_update(
                    self._make_update("my_addon"),
                    ssh,
                    notifier,
                    gate,
                    _poll=fake_poll,
                    ha_rest_client=_FailingRestClient(),
                )
            )

        assert result is False
        assert poll_called == [], "poll must not be called when REST trigger fails"
        assert notifier.sent[0]["payload"]["success"] is False

    def test_timeout_on_service_call_falls_through_to_poll(self):
        """httpx.TimeoutException must not abort — fall through to the poll."""
        import httpx

        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        poll_called = []

        class _TimeoutRestClient(FakeHARestClient):
            async def call_service(self, domain, service, payload):
                raise httpx.ReadTimeout("")

        async def fake_poll(slug, version, client, timeout_seconds=180):
            poll_called.append(slug)
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_addon_update(
                    self._make_update("file_editor"),
                    ssh,
                    notifier,
                    gate,
                    _poll=fake_poll,
                    ha_rest_client=_TimeoutRestClient(),
                )
            )

        assert (
            result is True
        ), "poll result must propagate even when service call timed out"
        assert poll_called == ["file_editor"], "poll must be called after timeout"
        assert notifier.sent[0]["payload"]["success"] is True

    def test_non_timeout_exception_sends_failure_card(self):
        """Non-timeout exceptions abort immediately without calling the poll."""
        from ha_update_manager import execute_addon_update
        from utils.autonomy import FakeAutonomyGate
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()
        poll_called = []

        class _ErrorRestClient(FakeHARestClient):
            async def call_service(self, domain, service, payload):
                raise RuntimeError("connection refused")

        async def fake_poll(slug, version, client, timeout_seconds=180):
            poll_called.append(slug)
            return True

        with self._patch_backup():
            result = asyncio.run(
                execute_addon_update(
                    self._make_update("file_editor"),
                    ssh,
                    notifier,
                    gate,
                    _poll=fake_poll,
                    ha_rest_client=_ErrorRestClient(),
                )
            )

        assert result is False
        assert poll_called == [], "poll must not be called on non-timeout error"
        assert notifier.sent[0]["payload"]["success"] is False


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

        with patch("ha_update_manager.execute_core_update", fake_core), patch(
            "ha_agent_advanced.is_reboot_required_active", return_value=False
        ):
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

        with patch("ha_update_manager.execute_os_update", fake_os), patch(
            "ha_agent_advanced.is_reboot_required_active", return_value=False
        ):
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

        async def fake_addon(update, ssh, notifier, gate, ha_rest_client=None):
            called.append("addon")
            return True

        with patch("ha_update_manager.execute_addon_update", fake_addon), patch(
            "ha_agent_advanced.is_reboot_required_active", return_value=False
        ):
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

            async def mock_offload(slug, ssh_client=None):
                pass

            orig_b = ha_agent_advanced.execute_remote_backup
            orig_r = ha_agent_advanced.record_backup_slug
            orig_o = ha_agent_advanced.offload_backup_to_local
            ha_agent_advanced.execute_remote_backup = mock_backup
            ha_agent_advanced.record_backup_slug = lambda s, name="": None
            ha_agent_advanced.offload_backup_to_local = mock_offload
            try:
                yield
            finally:
                ha_agent_advanced.execute_remote_backup = orig_b
                ha_agent_advanced.record_backup_slug = orig_r
                ha_agent_advanced.offload_backup_to_local = orig_o

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
    def test_http_login_hyphen_is_security_high(self):
        from ha_notification_manager import classify_notification

        category, severity = classify_notification("http-login")
        assert category == "security"
        assert severity == "HIGH"

    def test_http_login_underscore_falls_through(self):
        # HA sends "http-login" (hyphen); underscore variant is never sent by HA
        from ha_notification_manager import classify_notification

        category, severity = classify_notification("http_login")
        assert category == "other"
        assert severity == "MEDIUM"

    def test_ip_ban_is_security_high(self):
        from ha_notification_manager import classify_notification

        category, severity = classify_notification("ip-ban")
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

    def test_ha_created_at_stored(self, db_path):
        from ha_notification_manager import record_notification_seen

        ha_ts = 1722700876.0
        record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path, ha_created_at=ha_ts
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT ha_created_at FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert row is not None
        assert row[0] == ha_ts

    def test_ha_created_at_defaults_none(self, db_path):
        from ha_notification_manager import record_notification_seen

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT ha_created_at FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert row is not None
        assert row[0] is None

    def test_newer_ha_created_at_resets_hitl_sent_at(self, db_path):
        from ha_notification_manager import (
            record_notification_seen,
            mark_notification_hitl_sent,
        )

        # First occurrence: recorded and HITL card sent
        record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        mark_notification_hitl_sent("http_login", db_path=db_path)

        # Second occurrence with a newer ha_created_at: hitl_sent_at should be cleared
        record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path, ha_created_at=2000.0
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT hitl_sent_at, ha_created_at FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert (
            row[0] is None
        ), "hitl_sent_at must be reset for a new notification occurrence"
        assert row[1] == 2000.0

    def test_newer_ha_created_at_resets_first_seen_at(self, db_path):
        from ha_notification_manager import (
            record_notification_seen,
            mark_notification_hitl_sent,
        )

        record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        mark_notification_hitl_sent("http_login", db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            original_first_seen = conn.execute(
                "SELECT first_seen_at FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()[0]

        import time

        time.sleep(0.01)
        record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path, ha_created_at=2000.0
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT first_seen_at FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert (
            row[0] > original_first_seen
        ), "first_seen_at must be reset to now when a new occurrence is detected"

    def test_same_ha_created_at_does_not_reset_hitl_sent_at(self, db_path):
        from ha_notification_manager import (
            record_notification_seen,
            mark_notification_hitl_sent,
        )

        record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        mark_notification_hitl_sent("http_login", db_path=db_path)

        # Same ha_created_at: hitl_sent_at should remain set (already processed)
        record_notification_seen(
            "http_login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT hitl_sent_at FROM notification_history WHERE notification_id = ?",
                ("http_login",),
            ).fetchone()
        assert (
            row[0] is not None
        ), "hitl_sent_at must not be reset for same notification"

    def test_new_occurrence_resets_dismissed_at(self, db_path):
        from ha_notification_manager import (
            record_notification_seen,
            mark_notification_dismissed,
        )

        record_notification_seen(
            "http-login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        mark_notification_dismissed("http-login", dismissed_by="user", db_path=db_path)

        # New occurrence: dismissed_at and dismissed_by must be cleared
        record_notification_seen(
            "http-login", "security", "HIGH", db_path=db_path, ha_created_at=2000.0
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT dismissed_at, dismissed_by FROM notification_history WHERE notification_id = ?",
                ("http-login",),
            ).fetchone()
        assert row[0] is None, "dismissed_at must be reset for a new occurrence"
        assert row[1] is None, "dismissed_by must be reset for a new occurrence"

    def test_dismissed_and_same_timestamp_stays_dismissed(self, db_path):
        """Dismissed notification seen again with same ha_created_at should stay dismissed.

        This guards the race condition where the poller fires between Pueo calling the
        HA dismiss service and HA actually removing the notification from its active list.
        Same timestamp means it's the same occurrence, not a new one — dismissed_at must
        not be reset.
        """
        import sqlite3

        from ha_notification_manager import (
            mark_notification_dismissed,
            record_notification_seen,
        )

        record_notification_seen(
            "http-login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        mark_notification_dismissed("http-login", dismissed_by="user", db_path=db_path)

        # Poller fires before HA removes the notification — same ha_created_at.
        record_notification_seen(
            "http-login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT dismissed_at, dismissed_by FROM notification_history"
                " WHERE notification_id = ?",
                ("http-login",),
            ).fetchone()
        assert row[0] is not None, "dismissed_at must not be cleared for same timestamp"
        assert row[1] == "user", "dismissed_by must not be cleared for same timestamp"

    def test_dismissed_and_no_stored_timestamp_stays_dismissed(self, db_path):
        """Dismissed notification re-seen when stored ha_created_at is NULL stays dismissed.

        Without a stored timestamp we cannot confirm the incoming timestamp is newer, so
        we conservatively leave dismissed_at intact rather than risk re-opening a
        notification the user already handled.
        """
        import sqlite3

        from ha_notification_manager import (
            mark_notification_dismissed,
            record_notification_seen,
        )

        # Insert without ha_created_at so stored value is NULL.
        record_notification_seen(
            "http-login", "security", "HIGH", db_path=db_path, ha_created_at=None
        )
        mark_notification_dismissed("http-login", dismissed_by="user", db_path=db_path)

        record_notification_seen(
            "http-login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT dismissed_at FROM notification_history"
                " WHERE notification_id = ?",
                ("http-login",),
            ).fetchone()
        assert (
            row[0] is not None
        ), "dismissed_at must not be cleared when stored timestamp is NULL"

    def test_dismissed_older_timestamp_not_reopened(self, db_path):
        """Dismissed notification with an older ha_created_at is not treated as new."""
        import sqlite3

        from ha_notification_manager import (
            mark_notification_dismissed,
            record_notification_seen,
        )

        record_notification_seen(
            "http-login", "security", "HIGH", db_path=db_path, ha_created_at=2000.0
        )
        mark_notification_dismissed("http-login", dismissed_by="user", db_path=db_path)

        # Re-seen with an older timestamp — should not reopen (clock drift / replay).
        record_notification_seen(
            "http-login", "security", "HIGH", db_path=db_path, ha_created_at=1000.0
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT dismissed_at FROM notification_history WHERE notification_id = ?",
                ("http-login",),
            ).fetchone()
        assert (
            row[0] is not None
        ), "dismissed_at must not be cleared for an older timestamp"


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
            "ha_created_at",
        }
        assert expected.issubset(set(cols))

    def test_migration_is_idempotent(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.init_local_database()

    def test_migration_v10_adds_ha_created_at_column(self, db_path):
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        with sqlite3.connect(db_path) as conn:
            cols = [
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(notification_history)"
                ).fetchall()
            ]
        assert "ha_created_at" in cols


# ── ha_log_monitor — poll_for_updates ────────────────────────────────────────────


class TestPollForUpdates:
    @staticmethod
    def _make_update_entity(
        entity_id: str,
        installed: str = "2026.1.0",
        latest: str = "2026.2.0",
        update_available: bool = True,
    ) -> dict:
        return {
            "entity_id": entity_id,
            "state": "on" if update_available else "off",
            "attributes": {
                "installed_version": installed,
                "latest_version": latest,
                "release_url": None,
                "release_summary": None,
                "in_progress": False,
            },
        }

    @staticmethod
    def _one_shot_sleep() -> object:
        """No-op on first call (after the first poll), CancelledError on second."""
        call_count = [0]

        async def fake_sleep(seconds: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 2:
                raise asyncio.CancelledError()

        return fake_sleep

    def test_notifies_on_first_poll_without_sleeping_first(self, monkeypatch):
        """Updates available at startup must trigger a notification on the first iteration."""
        import asyncio as asyncio_mod

        from ha_log_monitor import poll_for_updates
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        entity = self._make_update_entity(
            "update.home_assistant_core_update",
            installed="2026.1.0",
            latest="2026.2.0",
        )
        client = FakeHARestClient(states=[entity])
        notifier = FakeNotifier()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())
        monkeypatch.setattr("ha_log_monitor.HA_UPDATE_NOTIFY_ON_AVAILABLE", True)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(poll_for_updates(ha_rest_client=client, notifier=notifier))

        assert len(notifier.sent) == 1
        assert "2026.2.0" in notifier.sent[0]["subject"]

    def test_no_duplicate_notification_for_same_entity(self, monkeypatch):
        """An entity already in _notified must not fire a second notification."""
        import asyncio as asyncio_mod

        from ha_log_monitor import poll_for_updates
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        entity = self._make_update_entity("update.home_assistant_core_update")
        client = FakeHARestClient(states=[entity])
        notifier = FakeNotifier()

        call_count = [0]

        async def fake_sleep(seconds: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 3:
                raise asyncio.CancelledError()

        monkeypatch.setattr(asyncio_mod, "sleep", fake_sleep)
        monkeypatch.setattr("ha_log_monitor.HA_UPDATE_NOTIFY_ON_AVAILABLE", True)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(poll_for_updates(ha_rest_client=client, notifier=notifier))

        assert len(notifier.sent) == 1

    def test_clears_notified_when_update_no_longer_available(self, monkeypatch):
        """Once an entity flips back to off, it should be removable from _notified."""
        import asyncio as asyncio_mod

        from ha_log_monitor import poll_for_updates
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        states: list[dict] = [
            self._make_update_entity("update.home_assistant_core_update")
        ]
        client = FakeHARestClient(states=states)
        notifier = FakeNotifier()

        call_count = [0]

        async def fake_sleep(seconds: float) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                # flip the entity off so the second poll clears _notified
                states[0]["state"] = "off"
                states[0]["attributes"]["installed_version"] = "2026.2.0"
            if call_count[0] >= 3:
                raise asyncio.CancelledError()

        monkeypatch.setattr(asyncio_mod, "sleep", fake_sleep)
        monkeypatch.setattr("ha_log_monitor.HA_UPDATE_NOTIFY_ON_AVAILABLE", True)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(poll_for_updates(ha_rest_client=client, notifier=notifier))

        # Only one notification sent (for the first poll when update was available)
        assert len(notifier.sent) == 1

    def test_notification_payload_has_card_type_update(self, monkeypatch):
        """Notification payload must include card_type='update' so the dashboard
        dispatch routes approve() to _execute_queued_update."""
        import asyncio as asyncio_mod

        from ha_log_monitor import poll_for_updates
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        entity = self._make_update_entity(
            "update.home_assistant_core_update",
            installed="2026.1.0",
            latest="2026.2.0",
        )
        client = FakeHARestClient(states=[entity])
        notifier = FakeNotifier()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())
        monkeypatch.setattr("ha_log_monitor.HA_UPDATE_NOTIFY_ON_AVAILABLE", True)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(poll_for_updates(ha_rest_client=client, notifier=notifier))

        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["card_type"] == "update"
        assert payload["component"] == "core"
        assert payload["installed_version"] == "2026.1.0"
        assert payload["latest_version"] == "2026.2.0"
        assert payload["risk"] == "CRITICAL"
        assert payload["severity"] == "CRITICAL"
        assert "breaking_changes" in payload

    def test_breaking_change_analysis_included_for_core_update(
        self, tmp_path, monkeypatch
    ):
        """For core updates, breaking-change analysis runs and populates the payload."""
        import asyncio as asyncio_mod
        import unittest.mock as mock

        from ha_update_manager import UpdateReadinessReport
        from ha_log_monitor import poll_for_updates
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeLLMClient

        entity = self._make_update_entity(
            "update.home_assistant_core_update",
            installed="2026.6.0",
            latest="2026.7.0",
        )
        client = FakeHARestClient(states=[entity])
        notifier = FakeNotifier()

        report = UpdateReadinessReport(
            target_version="2026.7.0",
            safe_to_update=False,
            breaking_changes=["Template syntax changed"],
            affected_config_keys=["template"],
            pueo_command_risks=["ha apps info"],
            recommendation="Review before updating.",
        )
        llm = FakeLLMClient(report.model_dump_json())

        # Pre-populate release notes cache so no WAN fetch is needed.
        notes_dir = tmp_path / "release_notes"
        notes_dir.mkdir()
        (notes_dir / "2026.7.0.txt").write_text(
            "## Breaking changes\n- Template syntax changed"
        )

        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())
        monkeypatch.setattr("ha_log_monitor.HA_UPDATE_NOTIFY_ON_AVAILABLE", True)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_updates(
                    ha_rest_client=client,
                    notifier=notifier,
                    llm_client=llm,
                    cache_dir=str(notes_dir),
                )
            )

        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["card_type"] == "update"
        assert payload["breaking_changes"] == ["Template syntax changed"]
        assert payload["safe_to_update"] is False
        assert payload["advisory"] == "Review before updating."

    def test_breaking_change_analysis_skipped_for_addon_update(self, monkeypatch):
        """Add-on updates do not run breaking-change analysis (only core does)."""
        import asyncio as asyncio_mod
        import unittest.mock as mock

        from ha_log_monitor import poll_for_updates
        from utils.ha_rest_client import FakeHARestClient
        from utils.notify import FakeNotifier

        entity = self._make_update_entity(
            "update.some_addon_update",
            installed="1.0.0",
            latest="1.1.0",
        )
        # Override entity attributes so component is recognized as an add-on
        entity["attributes"]["installed_version"] = "1.0.0"
        entity["attributes"]["latest_version"] = "1.1.0"
        client = FakeHARestClient(states=[entity])
        notifier = FakeNotifier()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())
        monkeypatch.setattr("ha_log_monitor.HA_UPDATE_NOTIFY_ON_AVAILABLE", True)

        with mock.patch("ha_update_manager.analyze_breaking_changes") as mock_analyze:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(poll_for_updates(ha_rest_client=client, notifier=notifier))

        mock_analyze.assert_not_called()
        payload = notifier.sent[0]["payload"]
        assert payload["card_type"] == "update"
        assert payload["risk"] == "MEDIUM"


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
            "notification_id": nid,
            "title": title,
            "message": message,
        }

    @staticmethod
    def _one_shot_sleep() -> object:
        """Fake asyncio.sleep: no-op on first call, raises CancelledError on second."""
        call_count = [0]

        async def fake_sleep(seconds: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 2:
                raise asyncio.CancelledError()

        return fake_sleep

    @staticmethod
    def _make_llm_client():
        from ha_notification_manager import _NotificationLLMOutput
        from utils.ollama_client import FakeLLMClient

        out = _NotificationLLMOutput(
            human_explanation="Test explanation.",
            recommended_action="Check it.",
            requires_hitl=True,
        )
        return FakeLLMClient(out.model_dump_json())

    def test_new_notification_triggers_notifier(self, db_path, monkeypatch):
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        notif = self._make_notification_entity(
            "http-login", "Login attempt", "Bad creds"
        )
        ws = FakeHAWebSocketClient(notifications=[notif])
        notifier = FakeNotifier()
        llm = self._make_llm_client()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ws,
                    notifier=notifier,
                    llm_client=llm,
                    ssh_client=FakeSSHClient(),
                    db_path=db_path,
                )
            )

        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["notification_id"] == "notif_http-login"
        assert payload["ha_notification_id"] == "http-login"
        assert payload["is_notification_card"] is True
        assert payload["category"] == "security"
        assert payload["severity"] == "HIGH"
        assert payload["human_explanation"] == "Test explanation."

    def test_duplicate_notification_not_resent(self, db_path, monkeypatch):
        """Notification already seen AND card already sent must not produce another card."""
        import asyncio as asyncio_mod
        from ha_notification_manager import (
            mark_notification_hitl_sent,
            record_notification_seen,
        )
        from ha_log_monitor import poll_for_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        mark_notification_hitl_sent("http_login", db_path=db_path)
        notif = self._make_notification_entity("http_login", "Login attempt")
        ws = FakeHAWebSocketClient(notifications=[notif])
        notifier = FakeNotifier()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ws, notifier=notifier, db_path=db_path
                )
            )

        assert len(notifier.sent) == 0

    def test_poll_failure_continues(self, db_path, monkeypatch):
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_notifications
        from utils.notify import FakeNotifier

        class ExplodingWSClient:
            async def get_persistent_notifications(self) -> list:
                raise RuntimeError("network down")

            async def get_device_registry(self) -> list:
                raise RuntimeError("network down")

        notifier = FakeNotifier()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ExplodingWSClient(),
                    notifier=notifier,
                    db_path=db_path,
                )
            )

        assert len(notifier.sent) == 0

    def test_unknown_notification_id_classified_as_other(self, db_path, monkeypatch):
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        notif = self._make_notification_entity(
            "some_integration_abc", message="Something failed"
        )
        ws = FakeHAWebSocketClient(notifications=[notif])
        notifier = FakeNotifier()
        llm = self._make_llm_client()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ws,
                    notifier=notifier,
                    llm_client=llm,
                    ssh_client=FakeSSHClient(),
                    db_path=db_path,
                )
            )

        assert len(notifier.sent) == 1
        assert notifier.sent[0]["payload"]["category"] == "other"
        assert notifier.sent[0]["payload"]["severity"] == "MEDIUM"

    def test_enrichment_failure_skips_notification(self, db_path, monkeypatch):
        """If enrich_and_analyze_notification raises, the notification is skipped."""
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier

        class ExplodingLLM:
            async def chat(self, model, messages, options, format):
                raise RuntimeError("LLM exploded")

        notif = self._make_notification_entity("http_login", "Login", "Bad creds")
        ws = FakeHAWebSocketClient(notifications=[notif])
        notifier = FakeNotifier()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ws,
                    notifier=notifier,
                    llm_client=ExplodingLLM(),
                    db_path=db_path,
                )
            )

        assert len(notifier.sent) == 0

    def test_hitl_sent_at_recorded_after_notification(self, db_path, monkeypatch):
        """mark_notification_hitl_sent is called after the notifier send."""
        import asyncio as asyncio_mod
        import sqlite3
        from ha_log_monitor import poll_for_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        notif = self._make_notification_entity(
            "invalid_config", "Config error", "Bad YAML"
        )
        ws = FakeHAWebSocketClient(notifications=[notif])
        notifier = FakeNotifier()
        llm = self._make_llm_client()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ws,
                    notifier=notifier,
                    llm_client=llm,
                    ssh_client=FakeSSHClient(),
                    db_path=db_path,
                )
            )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT hitl_sent_at FROM notification_history WHERE notification_id = ?",
                ("invalid_config",),
            ).fetchone()
        assert row[0] is not None

    def test_poll_sends_card_on_reappearing_notification(self, db_path, monkeypatch):
        """Re-appearing notification (same ID, newer ha_created_at) must generate a second card."""
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_notifications
        from ha_notification_manager import mark_notification_hitl_sent
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        created_at_1 = "2024-08-01T10:00:00+00:00"
        created_at_2 = "2024-08-02T10:00:00+00:00"

        notif_first = self._make_notification_entity(
            "http-login", "Login attempt", "Bad creds"
        )
        notif_first["created_at"] = created_at_1

        ws = FakeHAWebSocketClient(notifications=[notif_first])
        notifier = FakeNotifier()
        llm = self._make_llm_client()

        # --- First poll: notification appears for the first time ---
        call_count = [0]
        notifications_by_call = [[notif_first], []]

        async def fake_sleep_first(seconds: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 2:
                raise asyncio.CancelledError()

        monkeypatch.setattr(asyncio_mod, "sleep", fake_sleep_first)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ws,
                    notifier=notifier,
                    llm_client=llm,
                    ssh_client=FakeSSHClient(),
                    db_path=db_path,
                )
            )

        assert len(notifier.sent) == 1, "first occurrence must trigger a card"

        # Simulate HITL card sent (mark it in DB)
        mark_notification_hitl_sent("http-login", db_path=db_path)

        # --- Second poll: same notification re-appears with a newer created_at ---
        notif_second = self._make_notification_entity(
            "http-login", "Login attempt", "Bad creds again"
        )
        notif_second["created_at"] = created_at_2

        ws2 = FakeHAWebSocketClient(notifications=[notif_second])
        call_count2 = [0]

        async def fake_sleep_second(seconds: float) -> None:
            call_count2[0] += 1
            if call_count2[0] >= 2:
                raise asyncio.CancelledError()

        monkeypatch.setattr(asyncio_mod, "sleep", fake_sleep_second)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ws2,
                    notifier=notifier,
                    llm_client=llm,
                    ssh_client=FakeSSHClient(),
                    db_path=db_path,
                )
            )

        assert (
            len(notifier.sent) == 2
        ), "re-appearing notification must trigger a second card"

    def test_netalertx_name_included_in_enrichment(self, db_path, monkeypatch):
        """NetAlertX device name must appear in enriched_context when a client is provided."""
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient

        class FakeNetAlertXClient:
            async def get_devices(self):
                return [{"devLastIP": "10.0.0.168", "devName": "iPhony 5G"}]

        notif = self._make_notification_entity(
            "http-login",
            "Login attempt failed",
            "Login attempt or request with invalid authentication from 10.0.0.168 (10.0.0.168).",
        )
        ws = FakeHAWebSocketClient(notifications=[notif])
        notifier = FakeNotifier()
        llm = self._make_llm_client()
        monkeypatch.setattr(asyncio_mod, "sleep", self._one_shot_sleep())

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=ws,
                    notifier=notifier,
                    llm_client=llm,
                    ssh_client=FakeSSHClient(),
                    netalertx_client=FakeNetAlertXClient(),
                    db_path=db_path,
                )
            )

        assert len(notifier.sent) == 1
        ctx = notifier.sent[0]["payload"]["enriched_context"]
        assert ctx["netalertx_name"] == "iPhony 5G"
        assert ctx["is_known_device"] is True


# ── ha_notification_manager — extract_ip_from_message ────────────────────────────


class TestExtractIpFromMessage:
    def test_extracts_ip_after_from(self):
        from ha_notification_manager import extract_ip_from_message

        ip = extract_ip_from_message("Invalid login attempt from 192.168.1.42")
        assert ip == "192.168.1.42"

    def test_returns_none_when_no_ip(self):
        from ha_notification_manager import extract_ip_from_message

        assert extract_ip_from_message("Bad credentials supplied") is None

    def test_returns_none_for_empty_string(self):
        from ha_notification_manager import extract_ip_from_message

        assert extract_ip_from_message("") is None

    def test_extracts_first_ip_when_multiple(self):
        from ha_notification_manager import extract_ip_from_message

        ip = extract_ip_from_message("Login from 10.0.0.1 and also from 10.0.0.2")
        assert ip == "10.0.0.1"

    def test_ignores_ip_not_preceded_by_from(self):
        from ha_notification_manager import extract_ip_from_message

        assert extract_ip_from_message("Source: 192.168.1.1 connected") is None


# ── ha_notification_manager — enrich_http_login ──────────────────────────────────


class TestEnrichHttpLogin:
    def test_netalertx_match_sets_known_device(self):
        from ha_notification_manager import enrich_http_login

        class FakeNAX:
            async def get_devices(self):
                return [{"devLastIP": "192.168.1.42", "devName": "Andy's Phone"}]

        ctx = asyncio.run(enrich_http_login("192.168.1.42", netalertx_client=FakeNAX()))
        assert ctx["netalertx_name"] == "Andy's Phone"
        assert ctx["is_known_device"] is True

    def test_ws_match_sets_known_device(self):
        from ha_notification_manager import enrich_http_login
        from utils.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient(
            devices=[
                {
                    "id": "dev1",
                    "name": "Andy's Phone",
                    "name_by_user": "Andy's Phone",
                    "connections": [["ip", "192.168.1.42"]],
                }
            ]
        )
        ctx = asyncio.run(enrich_http_login("192.168.1.42", ws_client=ws))
        assert ctx["ha_device_name"] == "Andy's Phone"
        assert ctx["is_known_device"] is True
        assert ws.calls == ["get_device_registry"]

    def test_no_match_is_unknown_device(self):
        from ha_notification_manager import enrich_http_login

        class FakeNAX:
            async def get_devices(self):
                return [{"devLastIP": "10.0.0.1", "devName": "Other Device"}]

        ctx = asyncio.run(
            enrich_http_login(
                "192.168.1.99",
                netalertx_client=FakeNAX(),
            )
        )
        assert ctx["is_known_device"] is False
        assert ctx["netalertx_name"] is None

    def test_netalertx_failure_continues(self):
        from ha_notification_manager import enrich_http_login

        class ExplodingNAX:
            async def get_devices(self):
                raise RuntimeError("NetAlertX down")

        ctx = asyncio.run(
            enrich_http_login("192.168.1.1", netalertx_client=ExplodingNAX())
        )
        assert ctx["netalertx_name"] is None

    def test_ws_failure_continues(self):
        from ha_notification_manager import enrich_http_login

        class ExplodingWS:
            async def get_device_registry(self):
                raise RuntimeError("WS down")

        ctx = asyncio.run(enrich_http_login("192.168.1.1", ws_client=ExplodingWS()))
        assert ctx["ha_device_name"] is None

    def test_source_ip_always_in_context(self):
        from ha_notification_manager import enrich_http_login

        ctx = asyncio.run(enrich_http_login("1.2.3.4"))
        assert ctx["source_ip"] == "1.2.3.4"

    def test_ws_prefers_name_by_user_over_name(self):
        from ha_notification_manager import enrich_http_login
        from utils.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient(
            devices=[
                {
                    "id": "dev1",
                    "name": "Generic Name",
                    "name_by_user": "My Custom Name",
                    "connections": [["ip", "192.168.1.5"]],
                }
            ]
        )
        ctx = asyncio.run(enrich_http_login("192.168.1.5", ws_client=ws))
        assert ctx["ha_device_name"] == "My Custom Name"

    def test_ws_falls_back_to_name_when_no_name_by_user(self):
        from ha_notification_manager import enrich_http_login
        from utils.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient(
            devices=[
                {
                    "id": "dev1",
                    "name": "Fallback Name",
                    "name_by_user": None,
                    "connections": [["ip", "192.168.1.6"]],
                }
            ]
        )
        ctx = asyncio.run(enrich_http_login("192.168.1.6", ws_client=ws))
        assert ctx["ha_device_name"] == "Fallback Name"

    def test_arp_mac_added_to_context(self, monkeypatch):
        import ha_notification_manager
        from ha_notification_manager import _get_arp_info

        class _FakeProc:
            async def communicate(self):
                return (b"? (10.0.0.168) at 52:ea:9d:13:80:c8 on en0", b"")

        async def fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(
            ha_notification_manager.asyncio, "create_subprocess_exec", fake_exec
        )
        result = asyncio.run(_get_arp_info("10.0.0.168"))
        assert result["mac_address"] == "52:ea:9d:13:80:c8"
        assert result["mac_is_randomized"] is True
        assert result["mac_vendor"] is None

    def test_arp_non_randomized_mac(self, monkeypatch):
        import ha_notification_manager
        from ha_notification_manager import _get_arp_info

        class _FakeProc:
            async def communicate(self):
                return (b"? (10.0.0.1) at ac:bc:32:ab:cd:ef on en0", b"")

        async def fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(
            ha_notification_manager.asyncio, "create_subprocess_exec", fake_exec
        )
        result = asyncio.run(_get_arp_info("10.0.0.1"))
        assert result["mac_address"] == "ac:bc:32:ab:cd:ef"
        assert result["mac_is_randomized"] is False

    def test_arp_no_mac_returns_none_fields(self, monkeypatch):
        import ha_notification_manager
        from ha_notification_manager import _get_arp_info

        class _FakeProc:
            async def communicate(self):
                return (b"no entry for 10.0.0.200", b"")

        async def fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(
            ha_notification_manager.asyncio, "create_subprocess_exec", fake_exec
        )
        result = asyncio.run(_get_arp_info("10.0.0.200"))
        assert result["mac_address"] is None
        assert result["mac_is_randomized"] is None
        assert result["mac_vendor"] is None

    def test_dhcp_hostname_added_when_ssh_succeeds(self, monkeypatch):
        import ha_notification_manager
        from ha_notification_manager import _try_router_dhcp_hostname

        dhcp_output = b"1234567890 aa:bb:cc:dd:ee:ff 10.0.0.50 android-phone *\n"

        class _FakeProc:
            async def communicate(self):
                return (dhcp_output, b"")

        async def fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(
            ha_notification_manager.asyncio, "create_subprocess_exec", fake_exec
        )
        monkeypatch.setattr(
            ha_notification_manager.asyncio, "wait_for", lambda coro, timeout: coro
        )
        result = asyncio.run(_try_router_dhcp_hostname("10.0.0.1", "10.0.0.50"))
        assert result == "android-phone"

    def test_dhcp_probe_silent_on_failure(self, monkeypatch):
        import ha_notification_manager
        from ha_notification_manager import _try_router_dhcp_hostname

        async def fake_exec(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(
            ha_notification_manager.asyncio, "create_subprocess_exec", fake_exec
        )
        monkeypatch.setattr(
            ha_notification_manager.asyncio, "wait_for", lambda coro, timeout: coro
        )
        result = asyncio.run(_try_router_dhcp_hostname("10.0.0.1", "10.0.0.99"))
        assert result is None


# ── ha_notification_manager — analyze_notification ───────────────────────────────


class TestAnalyzeNotification:
    @staticmethod
    def _make_llm_client(requires_hitl: bool = True):
        from ha_notification_manager import _NotificationLLMOutput
        from utils.ollama_client import FakeLLMClient

        out = _NotificationLLMOutput(
            human_explanation="Someone tried to log in.",
            recommended_action="Change your password.",
            requires_hitl=requires_hitl,
        )
        return FakeLLMClient(out.model_dump_json())

    def test_returns_notification_analysis(self):
        from ha_notification_manager import NotificationAnalysis, analyze_notification

        result = asyncio.run(
            analyze_notification(
                "http_login",
                "Login attempt",
                "Bad creds from 192.168.1.1",
                {"source_ip": "192.168.1.1", "is_known_device": False},
                "",
                llm_client=self._make_llm_client(),
                category="security",
                severity="CRITICAL",
            )
        )
        assert isinstance(result, NotificationAnalysis)
        assert result.notification_id == "http_login"
        assert result.category == "security"
        assert result.severity == "CRITICAL"
        assert result.human_explanation == "Someone tried to log in."
        assert result.requires_hitl is True

    def test_passes_config_content_truncated(self):
        from ha_notification_manager import _NotificationLLMOutput, analyze_notification
        from utils.ollama_client import FakeLLMClient

        llm = FakeLLMClient(
            _NotificationLLMOutput(
                human_explanation="Config broken.",
                recommended_action="Fix it.",
                requires_hitl=True,
            ).model_dump_json()
        )
        result = asyncio.run(
            analyze_notification(
                "invalid_config",
                None,
                "Error in sensor platform",
                {},
                "homeassistant:\n  name: Home\n",
                llm_client=llm,
                category="config_error",
                severity="HIGH",
            )
        )
        assert result.original_message == "Error in sensor platform"
        assert result.original_title is None
        assert len(llm.calls) == 1
        user_msg = llm.calls[0]["messages"][1]["content"]
        assert "configuration.yaml" in user_msg

    def test_enriched_context_in_payload(self):
        from ha_notification_manager import analyze_notification

        ctx = {"source_ip": "10.0.0.1", "is_known_device": True}
        result = asyncio.run(
            analyze_notification(
                "http_login", None, "msg", ctx, "", llm_client=self._make_llm_client()
            )
        )
        assert result.enriched_context == ctx

    def test_requires_hitl_false_propagates(self):
        from ha_notification_manager import analyze_notification

        result = asyncio.run(
            analyze_notification(
                "other",
                None,
                "msg",
                {},
                "",
                llm_client=self._make_llm_client(requires_hitl=False),
            )
        )
        assert result.requires_hitl is False


# ── ha_notification_manager — enrich_and_analyze_notification ────────────────────


class TestEnrichAndAnalyzeNotification:
    @staticmethod
    def _make_llm_client(requires_hitl: bool = True):
        from ha_notification_manager import _NotificationLLMOutput
        from utils.ollama_client import FakeLLMClient

        out = _NotificationLLMOutput(
            human_explanation="Explanation.",
            recommended_action="Action.",
            requires_hitl=requires_hitl,
        )
        return FakeLLMClient(out.model_dump_json())

    def test_http_login_unknown_ip_escalated_to_critical(self):
        from ha_notification_manager import enrich_and_analyze_notification

        class NeverMatchNAX:
            async def get_devices(self):
                return []

        result = asyncio.run(
            enrich_and_analyze_notification(
                "http-login",
                "Login attempt",
                "Invalid auth from 192.168.1.99",
                llm_client=self._make_llm_client(),
                netalertx_client=NeverMatchNAX(),
            )
        )
        assert result.severity == "CRITICAL"
        assert result.enriched_context["source_ip"] == "192.168.1.99"
        assert result.enriched_context["is_known_device"] is False

    def test_http_login_known_ip_stays_high(self):
        from ha_notification_manager import enrich_and_analyze_notification

        class MatchingNAX:
            async def get_devices(self):
                return [{"devLastIP": "192.168.1.42", "devName": "Laptop"}]

        result = asyncio.run(
            enrich_and_analyze_notification(
                "http-login",
                "Login attempt",
                "Invalid auth from 192.168.1.42",
                llm_client=self._make_llm_client(),
                netalertx_client=MatchingNAX(),
            )
        )
        assert result.severity == "HIGH"
        assert result.enriched_context["netalertx_name"] == "Laptop"

    def test_http_login_no_ip_in_message_stays_high(self):
        from ha_notification_manager import enrich_and_analyze_notification

        result = asyncio.run(
            enrich_and_analyze_notification(
                "http-login",
                "Login attempt",
                "Bad credentials supplied",
                llm_client=self._make_llm_client(),
            )
        )
        assert result.severity == "HIGH"
        assert result.enriched_context == {}

    def test_invalid_config_includes_config_content_in_llm(self):
        from ha_notification_manager import enrich_and_analyze_notification
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        llm = self._make_llm_client()
        asyncio.run(
            enrich_and_analyze_notification(
                "invalid_config",
                None,
                "Configuration error",
                ssh_client=ssh,
                llm_client=llm,
            )
        )
        assert len(llm.calls) == 1
        user_msg = llm.calls[0]["messages"][1]["content"]
        assert "configuration.yaml" in user_msg

    def test_invalid_config_ssh_failure_continues(self):
        from ha_notification_manager import enrich_and_analyze_notification

        class ExplodingSSH:
            async def read_file(self, path):
                raise RuntimeError("SSH down")

            async def write_file(self, path, content):
                raise RuntimeError("SSH down")

            async def download_file(self, remote, local):
                raise RuntimeError("SSH down")

            async def run(self, cmd, check=False):
                raise RuntimeError("SSH down")

            def stream_lines(self, cmd):
                raise RuntimeError("SSH down")

        result = asyncio.run(
            enrich_and_analyze_notification(
                "invalid_config",
                None,
                "Config error",
                ssh_client=ExplodingSSH(),
                llm_client=self._make_llm_client(),
            )
        )
        assert result.notification_id == "invalid_config"

    def test_other_notification_no_enrichment(self):
        from ha_notification_manager import enrich_and_analyze_notification

        result = asyncio.run(
            enrich_and_analyze_notification(
                "integration_error",
                "Integration failed",
                "Component X failed to load",
                llm_client=self._make_llm_client(),
            )
        )
        assert result.category == "other"
        assert result.severity == "MEDIUM"
        assert result.enriched_context == {}


# ── utils/ha_ws_client — FakeHAWebSocketClient ───────────────────────────────────


class TestFakeHAWebSocketClient:
    def test_returns_configured_devices(self):
        from utils.ha_ws_client import FakeHAWebSocketClient

        devices = [{"id": "dev1", "name": "Test", "connections": [["ip", "1.2.3.4"]]}]
        ws = FakeHAWebSocketClient(devices=devices)
        result = asyncio.run(ws.get_device_registry())
        assert result == devices

    def test_empty_by_default(self):
        from utils.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient()
        result = asyncio.run(ws.get_device_registry())
        assert result == []

    def test_records_calls(self):
        from utils.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient()
        asyncio.run(ws.get_device_registry())
        asyncio.run(ws.get_device_registry())
        assert ws.calls == ["get_device_registry", "get_device_registry"]

    def test_returns_independent_copy(self):
        from utils.ha_ws_client import FakeHAWebSocketClient

        devices = [{"id": "dev1"}]
        ws = FakeHAWebSocketClient(devices=devices)
        result = asyncio.run(ws.get_device_registry())
        result.append({"id": "dev2"})
        assert len(asyncio.run(ws.get_device_registry())) == 1

    def test_get_persistent_notifications_returns_configured(self):
        from utils.ha_ws_client import FakeHAWebSocketClient

        notifs = [{"notification_id": "http_login", "message": "Bad login"}]
        ws = FakeHAWebSocketClient(notifications=notifs)
        result = asyncio.run(ws.get_persistent_notifications())
        assert result == notifs
        assert ws.calls == ["get_persistent_notifications"]

    def test_get_persistent_notifications_empty_by_default(self):
        from utils.ha_ws_client import FakeHAWebSocketClient

        ws = FakeHAWebSocketClient()
        result = asyncio.run(ws.get_persistent_notifications())
        assert result == []


# ── ha_notification_manager — _format_notification_subject ───────────────────────


class TestFormatNotificationSubject:
    def _make_analysis(
        self, notification_id, enriched_context=None, original_title=None
    ):
        from ha_notification_manager import NotificationAnalysis

        return NotificationAnalysis(
            notification_id=notification_id,
            category="security",
            severity="HIGH",
            original_title=original_title,
            original_message="msg",
            human_explanation="explain",
            enriched_context=enriched_context or {},
            recommended_action="act",
            requires_hitl=True,
        )

    def test_http_login_unknown_device(self):
        from ha_notification_manager import _format_notification_subject

        analysis = self._make_analysis(
            "http-login",
            enriched_context={
                "source_ip": "1.2.3.4",
                "hostname": None,
                "netalertx_name": None,
                "ha_device_name": None,
                "is_known_device": False,
            },
        )
        subject = _format_notification_subject(analysis)
        assert "Unknown source IP" in subject

    def test_http_login_netalertx_name(self):
        from ha_notification_manager import _format_notification_subject

        analysis = self._make_analysis(
            "http-login",
            enriched_context={
                "source_ip": "192.168.1.10",
                "hostname": None,
                "netalertx_name": "Andy's Phone",
                "ha_device_name": None,
                "is_known_device": True,
            },
        )
        subject = _format_notification_subject(analysis)
        assert "Andy's Phone" in subject

    def test_http_login_ha_device_name_fallback(self):
        from ha_notification_manager import _format_notification_subject

        analysis = self._make_analysis(
            "http-login",
            enriched_context={
                "source_ip": "192.168.1.10",
                "hostname": None,
                "netalertx_name": None,
                "ha_device_name": "Living Room Hub",
                "is_known_device": True,
            },
        )
        subject = _format_notification_subject(analysis)
        assert "Living Room Hub" in subject

    def test_http_login_hostname_fallback(self):
        from ha_notification_manager import _format_notification_subject

        analysis = self._make_analysis(
            "http-login",
            enriched_context={
                "source_ip": "192.168.1.20",
                "hostname": "my-laptop.local",
                "netalertx_name": None,
                "ha_device_name": None,
                "is_known_device": True,
            },
        )
        subject = _format_notification_subject(analysis)
        assert "my-laptop.local" in subject

    def test_invalid_config(self):
        from ha_notification_manager import _format_notification_subject

        analysis = self._make_analysis("invalid_config")
        subject = _format_notification_subject(analysis)
        assert "Configuration" in subject

    def test_other_with_title(self):
        from ha_notification_manager import _format_notification_subject

        analysis = self._make_analysis("custom_integration", original_title="My Title")
        subject = _format_notification_subject(analysis)
        assert subject == "My Title"

    def test_other_without_title(self):
        from ha_notification_manager import _format_notification_subject

        analysis = self._make_analysis("some_random_notification_id")
        subject = _format_notification_subject(analysis)
        assert "some_random_notification_id" in subject


# ── ha_notification_manager — run_notifications ───────────────────────────────────


class TestRunNotifications:
    @pytest.fixture
    def db_path(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def _make_llm_client(self):
        from ha_notification_manager import _NotificationLLMOutput
        from utils.ollama_client import FakeLLMClient

        return FakeLLMClient(
            _NotificationLLMOutput(
                human_explanation="Someone tried to log in.",
                recommended_action="Check your logs.",
                requires_hitl=True,
            ).model_dump_json()
        )

    def test_sends_card_for_new_notification(self, db_path):
        from ha_notification_manager import run_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier

        ws = FakeHAWebSocketClient(
            notifications=[
                {
                    "notification_id": "http_login",
                    "title": "Login attempt",
                    "message": "Invalid login from 1.2.3.4",
                    "status": "unread",
                }
            ]
        )
        notifier = FakeNotifier()
        count = asyncio.run(
            run_notifications(
                ws_client=ws,
                llm_client=self._make_llm_client(),
                notifier=notifier,
                db_path=db_path,
            )
        )
        assert count == 1
        assert len(notifier.sent) == 1
        sent = notifier.sent[0]
        assert sent["payload"]["is_notification_card"] is True
        assert sent["payload"]["ha_notification_id"] == "http_login"

    def test_skips_notification_when_hitl_already_sent(self, db_path):
        from ha_notification_manager import (
            mark_notification_hitl_sent,
            record_notification_seen,
            run_notifications,
        )
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier

        record_notification_seen("http_login", "security", "HIGH", db_path=db_path)
        mark_notification_hitl_sent("http_login", db_path=db_path)

        ws = FakeHAWebSocketClient(
            notifications=[
                {
                    "notification_id": "http_login",
                    "title": None,
                    "message": "Invalid login from 1.2.3.4",
                    "status": "unread",
                }
            ]
        )
        notifier = FakeNotifier()
        count = asyncio.run(
            run_notifications(
                ws_client=ws,
                llm_client=self._make_llm_client(),
                notifier=notifier,
                db_path=db_path,
            )
        )
        assert count == 0
        assert len(notifier.sent) == 0

    def test_no_entities_returns_zero(self, db_path):
        from ha_notification_manager import run_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier

        ws = FakeHAWebSocketClient(notifications=[])
        notifier = FakeNotifier()
        count = asyncio.run(
            run_notifications(
                ws_client=ws,
                llm_client=self._make_llm_client(),
                notifier=notifier,
                db_path=db_path,
            )
        )
        assert count == 0

    def test_poll_failure_returns_zero(self, db_path):
        from ha_notification_manager import run_notifications
        from utils.notify import FakeNotifier

        class FailingWSClient:
            async def get_device_registry(self):
                raise RuntimeError("connection refused")

            async def get_persistent_notifications(self):
                raise RuntimeError("connection refused")

        notifier = FakeNotifier()
        count = asyncio.run(
            run_notifications(
                ws_client=FailingWSClient(),
                llm_client=self._make_llm_client(),
                notifier=notifier,
                db_path=db_path,
            )
        )
        assert count == 0

    def test_card_id_uses_notif_prefix(self, db_path):
        from ha_notification_manager import run_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier

        ws = FakeHAWebSocketClient(
            notifications=[
                {
                    "notification_id": "invalid_config",
                    "title": "Config Error",
                    "message": "YAML parse error",
                    "status": "unread",
                }
            ]
        )
        notifier = FakeNotifier()
        asyncio.run(
            run_notifications(
                ws_client=ws,
                llm_client=self._make_llm_client(),
                notifier=notifier,
                db_path=db_path,
            )
        )
        assert notifier.sent[0]["payload"]["notification_id"] == "notif_invalid_config"

    def test_unknown_ip_subject_in_card(self, db_path, monkeypatch):
        import socket

        import ha_notification_manager
        from ha_notification_manager import run_notifications
        from utils.ha_ws_client import FakeHAWebSocketClient
        from utils.notify import FakeNotifier

        monkeypatch.setattr(
            socket, "gethostbyaddr", lambda ip: (_ for _ in ()).throw(socket.herror())
        )

        ws = FakeHAWebSocketClient(
            notifications=[
                {
                    "notification_id": "http-login",
                    "title": None,
                    "message": "Invalid login from 8.8.8.8",
                    "status": "unread",
                }
            ]
        )
        notifier = FakeNotifier()
        asyncio.run(
            run_notifications(
                ws_client=ws,
                llm_client=self._make_llm_client(),
                notifier=notifier,
                db_path=db_path,
            )
        )
        assert len(notifier.sent) == 1
        assert "Unknown source IP" in notifier.sent[0]["subject"]


# ── Tool Registry — item 42 ─────────────────────────────────────────────────────


class TestToolRegistrySchemas:
    def test_tool_call_valid(self):
        from utils.tool_registry import ToolCall

        tc = ToolCall(
            name="read_config", arguments={"path": "/config/configuration.yaml"}
        )
        assert tc.name == "read_config"
        assert tc.arguments["path"] == "/config/configuration.yaml"

    def test_tool_call_empty_arguments(self):
        from utils.tool_registry import ToolCall

        tc = ToolCall(name="verify_fix")
        assert tc.arguments == {}

    def test_tool_call_missing_name_raises(self):
        from pydantic import ValidationError
        from utils.tool_registry import ToolCall

        with pytest.raises(ValidationError):
            ToolCall()  # type: ignore[call-arg]

    def test_tool_call_json_round_trip(self):
        from utils.tool_registry import ToolCall

        orig = ToolCall(
            name="apply_fix",
            arguments={"yaml_content": "key: val", "description": "test"},
        )
        restored = ToolCall.model_validate_json(orig.model_dump_json())
        assert restored == orig

    def test_tool_result_valid(self):
        from utils.tool_registry import ToolResult

        r = ToolResult(tool_name="read_config", success=True, output="yaml: content")
        assert r.success
        assert r.error is None

    def test_tool_result_failure(self):
        from utils.tool_registry import ToolResult

        r = ToolResult(
            tool_name="read_config", success=False, output="", error="SSH timeout"
        )
        assert not r.success
        assert r.error == "SSH timeout"

    def test_tool_result_json_round_trip(self):
        from utils.tool_registry import ToolResult

        orig = ToolResult(
            tool_name="apply_fix", success=True, output="done", error=None
        )
        restored = ToolResult.model_validate_json(orig.model_dump_json())
        assert restored == orig

    def test_tool_result_awaiting_approval_defaults_false(self):
        from utils.tool_registry import ToolResult

        r = ToolResult(tool_name="apply_fix", success=False, output="queued")
        assert r.awaiting_approval is False

    def test_tool_result_awaiting_approval_set(self):
        from utils.tool_registry import ToolResult

        r = ToolResult(
            tool_name="apply_fix",
            success=False,
            output="queued",
            awaiting_approval=True,
        )
        assert r.awaiting_approval is True

    def test_tool_result_awaiting_approval_round_trip(self):
        from utils.tool_registry import ToolResult

        orig = ToolResult(
            tool_name="apply_fix",
            success=False,
            output="queued",
            awaiting_approval=True,
        )
        restored = ToolResult.model_validate_json(orig.model_dump_json())
        assert restored.awaiting_approval is True

    def test_agent_loop_outcome_includes_awaiting_approval(self):
        from utils.tool_registry import AgentLoopResult

        r = AgentLoopResult(outcome="awaiting_approval", steps=[])
        assert r.outcome == "awaiting_approval"

    def test_agent_step_valid(self):
        from utils.tool_registry import AgentStep, ToolCall, ToolResult

        step = AgentStep(
            step_number=1,
            tool_call=ToolCall(name="read_config", arguments={}),
            tool_result=ToolResult(
                tool_name="read_config", success=True, output="yaml: {}"
            ),
            timestamp=0.5,
        )
        assert step.step_number == 1
        assert step.tool_call.name == "read_config"

    def test_agent_step_json_round_trip(self):
        from utils.tool_registry import AgentStep, ToolCall, ToolResult

        orig = AgentStep(
            step_number=2,
            tool_call=ToolCall(name="verify_fix"),
            tool_result=ToolResult(
                tool_name="verify_fix", success=True, output="passed"
            ),
            timestamp=1.0,
        )
        restored = AgentStep.model_validate_json(orig.model_dump_json())
        assert restored == orig

    def test_agent_loop_result_valid(self):
        from utils.tool_registry import AgentLoopResult

        r = AgentLoopResult(outcome="success", steps=[], episode_stub={"summary": "ok"})
        assert r.outcome == "success"

    def test_agent_loop_result_json_round_trip(self):
        from utils.tool_registry import AgentLoopResult

        orig = AgentLoopResult(outcome="exhausted", steps=[], episode_stub=None)
        restored = AgentLoopResult.model_validate_json(orig.model_dump_json())
        assert restored == orig

    def test_tool_definition_valid(self):
        from utils.tool_registry import ToolDefinition

        td = ToolDefinition(
            name="my_tool",
            description="does a thing",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        assert td.name == "my_tool"

    def test_tool_registry_register_and_names(self):
        from utils.tool_registry import ToolDefinition, ToolRegistry

        reg = ToolRegistry()
        reg.register(ToolDefinition(name="t1", description="d1", parameters={}))
        reg.register(ToolDefinition(name="t2", description="d2", parameters={}))
        assert set(reg.names()) == {"t1", "t2"}

    def test_tool_registry_get_ollama_tools(self):
        from utils.tool_registry import ToolDefinition, ToolRegistry

        reg = ToolRegistry()
        reg.register(
            ToolDefinition(
                name="read_config",
                description="reads config",
                parameters={"type": "object", "properties": {}, "required": []},
            )
        )
        tools = reg.get_ollama_tools()
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "read_config"

    def test_tool_registry_contains(self):
        from utils.tool_registry import ToolDefinition, ToolRegistry

        reg = ToolRegistry()
        reg.register(
            ToolDefinition(name="finish_repair", description="d", parameters={})
        )
        assert "finish_repair" in reg
        assert "unknown_tool" not in reg

    def test_build_ha_tool_registry_has_all_standard_tools(self):
        from utils.tool_registry import build_ha_tool_registry

        reg = build_ha_tool_registry()
        for name in (
            "read_config",
            "read_logs",
            "run_ha_command",
            "read_file",
            "query_netalertx",
            "apply_fix",
            "verify_fix",
            "finish_repair",
            "query_knowledge",
        ):
            assert name in reg, f"Missing tool: {name}"

    def test_build_netalertx_tool_registry_has_netalertx_tools(self):
        from utils.tool_registry import build_netalertx_tool_registry

        reg = build_netalertx_tool_registry()
        assert "restart_netalertx" in reg
        assert "rewrite_netalertx_conf" in reg
        assert "finish_repair" in reg


# ── Tool Executor — item 43 ─────────────────────────────────────────────────────


class TestToolExecutor:
    def _make_executor(self, ssh=None, gate=None, notifier=None):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor

        return ToolExecutor(
            ha_ssh_client=ssh or FakeSSHClient(),
            gate=gate
            or FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=notifier or FakeNotifier(approve=True),
        )

    def test_unknown_tool_returns_error(self):
        from utils.tool_registry import ToolCall

        executor = self._make_executor()
        result = asyncio.run(executor.execute(ToolCall(name="does_not_exist")))
        assert not result.success
        assert "Unknown tool" in result.error

    def test_read_config_success(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        executor = self._make_executor(ssh=ssh)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="read_config", arguments={"path": "/config/configuration.yaml"}
                )
            )
        )
        assert result.success
        assert result.tool_name == "read_config"

    def test_read_config_ssh_failure(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        # FakeSSHClient with no file_contents raises FileNotFoundError for any path
        ssh = FakeSSHClient()
        executor = self._make_executor(ssh=ssh)
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="read_config", arguments={"path": "/config/configuration.yaml"}
                )
            )
        )
        assert not result.success
        assert result.error is not None

    def test_run_ha_command_allowed(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient()
        executor = self._make_executor(ssh=ssh)
        result = asyncio.run(
            executor.execute(
                ToolCall(name="run_ha_command", arguments={"command": "ha core check"})
            )
        )
        assert result.tool_name == "run_ha_command"

    def test_run_ha_command_rejected_not_in_allowlist(self):
        from utils.tool_registry import ToolCall

        executor = self._make_executor()
        result = asyncio.run(
            executor.execute(
                ToolCall(name="run_ha_command", arguments={"command": "rm -rf /"})
            )
        )
        assert not result.success
        assert "not in allowlist" in result.error

    def test_read_file_allowed_path(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(file_contents={"/config/secrets.yaml": "token: abc"})
        executor = self._make_executor(ssh=ssh)
        result = asyncio.run(
            executor.execute(
                ToolCall(name="read_file", arguments={"path": "/config/secrets.yaml"})
            )
        )
        assert result.tool_name == "read_file"
        assert result.success

    def test_read_file_rejected_disallowed_path(self):
        from utils.tool_registry import ToolCall

        executor = self._make_executor()
        result = asyncio.run(
            executor.execute(
                ToolCall(name="read_file", arguments={"path": "/etc/passwd"})
            )
        )
        assert not result.success
        assert "not in allowed directories" in result.error

    def test_finish_repair_success(self):
        from utils.tool_registry import ToolCall

        executor = self._make_executor()
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="finish_repair",
                    arguments={"summary": "done", "action_taken": "no_fix_needed"},
                )
            )
        )
        assert result.success
        assert result.tool_name == "finish_repair"

    def test_query_knowledge_no_store_returns_error(self):
        from utils.tool_registry import ToolCall

        executor = self._make_executor()  # no knowledge_store injected
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="query_knowledge", arguments={"query": "breaking changes"}
                )
            )
        )
        assert not result.success
        assert "not configured" in result.error

    def test_apply_fix_once_per_loop_cap(self):
        """apply_fix rejected on second call within the same loop run."""
        from utils.tool_registry import ToolCall

        executor = self._make_executor()
        executor._apply_fix_used = True  # simulate first call already happened
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="apply_fix",
                    arguments={"yaml_content": "key: val", "description": "fix"},
                )
            )
        )
        assert not result.success
        assert "only be called once" in result.error

    def test_reset_clears_apply_fix_flag(self):
        from utils.tool_executor import ToolExecutor
        from utils.ssh_client import FakeSSHClient
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier

        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
        )
        executor._apply_fix_used = True
        executor.reset()
        assert not executor._apply_fix_used

    def test_query_netalertx_no_api_client(self):
        from utils.tool_registry import ToolCall

        executor = self._make_executor()  # no netalertx_api_client
        result = asyncio.run(
            executor.execute(
                ToolCall(name="query_netalertx", arguments={"query_type": "health"})
            )
        )
        assert not result.success
        assert "not configured" in result.error

    # -- read_logs ---------------------------------------------------------------

    def test_read_logs_success(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(
            command_results={"ha core logs": (0, "log line 1\nlog line 2", "")}
        )
        executor = self._make_executor(ssh=ssh)
        result = asyncio.run(
            executor.execute(ToolCall(name="read_logs", arguments={"lines": 50}))
        )
        assert result.success
        assert "log line" in result.output

    def test_read_logs_ssh_failure(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        class _RaisingSSH(FakeSSHClient):
            async def run(self, command, check=False):
                raise RuntimeError("SSH broken")

        executor = self._make_executor(ssh=_RaisingSSH())
        result = asyncio.run(executor.execute(ToolCall(name="read_logs", arguments={})))
        assert not result.success
        assert "SSH broken" in result.error

    def test_read_logs_default_lines(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(command_results={"ha core logs": (0, "output", "")})
        executor = self._make_executor(ssh=ssh)
        result = asyncio.run(executor.execute(ToolCall(name="read_logs", arguments={})))
        assert result.success

    # -- run_ha_command exception path -------------------------------------------

    def test_run_ha_command_ssh_exception(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        class _RaisingSSH(FakeSSHClient):
            async def run(self, command, check=False):
                raise ConnectionError("connection dropped")

        executor = self._make_executor(ssh=_RaisingSSH())
        result = asyncio.run(
            executor.execute(
                ToolCall(name="run_ha_command", arguments={"command": "ha core check"})
            )
        )
        assert not result.success
        assert "connection dropped" in result.error

    # -- read_file exception path ------------------------------------------------

    def test_read_file_ssh_exception(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        class _RaisingSSH(FakeSSHClient):
            async def read_file(self, path):
                raise PermissionError("denied")

        executor = self._make_executor(ssh=_RaisingSSH())
        result = asyncio.run(
            executor.execute(
                ToolCall(name="read_file", arguments={"path": "/config/secrets.yaml"})
            )
        )
        assert not result.success
        assert "denied" in result.error

    # -- verify_fix --------------------------------------------------------------

    def test_verify_fix_success(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(
            command_results={"ha core check": (0, "Configuration is valid!", "")}
        )
        executor = self._make_executor(ssh=ssh)
        result = asyncio.run(executor.execute(ToolCall(name="verify_fix")))
        assert result.success
        assert "passed" in result.output

    def test_verify_fix_failure(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(
            command_results={"ha core check": (1, "", "Invalid YAML on line 5")}
        )
        executor = self._make_executor(ssh=ssh)
        result = asyncio.run(executor.execute(ToolCall(name="verify_fix")))
        assert not result.success
        assert "ha core check failed" in result.error

    def test_verify_fix_ssh_exception(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        class _RaisingSSH(FakeSSHClient):
            async def run(self, command, check=False):
                raise TimeoutError("SSH timed out")

        executor = self._make_executor(ssh=_RaisingSSH())
        result = asyncio.run(executor.execute(ToolCall(name="verify_fix")))
        assert not result.success
        assert "timed out" in result.error

    # -- query_netalertx with api client -----------------------------------------

    def test_query_netalertx_health_with_client(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _FakeNAXApi:
            async def get_about(self):
                return {"status": "ok", "version": "26.7.1"}

            async def get_devices(self):
                return []

            async def get_events(self):
                return []

            async def trigger_scan(self):
                pass

        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            netalertx_api_client=_FakeNAXApi(),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="query_netalertx", arguments={"query_type": "health"})
            )
        )
        assert result.success
        assert "ok" in result.output

    def test_query_netalertx_devices(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _FakeNAXApi:
            async def get_about(self):
                return {}

            async def get_devices(self):
                return [{"mac": "AA:BB:CC:DD:EE:FF", "name": "my-device"}]

            async def get_events(self):
                return []

            async def trigger_scan(self):
                pass

        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            netalertx_api_client=_FakeNAXApi(),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="query_netalertx", arguments={"query_type": "devices"})
            )
        )
        assert result.success
        assert "my-device" in result.output

    def test_query_netalertx_events(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _FakeNAXApi:
            async def get_about(self):
                return {}

            async def get_devices(self):
                return []

            async def get_events(self):
                return [{"type": "new_device", "mac": "AA:BB:CC:DD:EE:FF"}]

            async def trigger_scan(self):
                pass

        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            netalertx_api_client=_FakeNAXApi(),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="query_netalertx", arguments={"query_type": "events"})
            )
        )
        assert result.success

    def test_query_netalertx_unknown_type(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _FakeNAXApi:
            async def get_about(self):
                return {}

            async def get_devices(self):
                return []

            async def get_events(self):
                return []

            async def trigger_scan(self):
                pass

        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            netalertx_api_client=_FakeNAXApi(),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="query_netalertx", arguments={"query_type": "unknown"})
            )
        )
        assert not result.success
        assert "Unknown query_type" in result.error

    def test_query_netalertx_api_exception(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _ErrorNAXApi:
            async def get_about(self):
                raise RuntimeError("API down")

            async def get_devices(self):
                return []

            async def get_events(self):
                return []

            async def trigger_scan(self):
                pass

        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            netalertx_api_client=_ErrorNAXApi(),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="query_netalertx", arguments={"query_type": "health"})
            )
        )
        assert not result.success
        assert "API down" in result.error

    # -- restart_netalertx -------------------------------------------------------

    def test_restart_netalertx_success(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(command_results={"docker restart": (0, "netalertx\n", "")})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
        )
        result = asyncio.run(executor.execute(ToolCall(name="restart_netalertx")))
        assert result.success

    def test_restart_netalertx_with_api_scan_trigger(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _FakeNAXApi:
            scan_triggered = False

            async def get_about(self):
                return {}

            async def get_devices(self):
                return []

            async def get_events(self):
                return []

            async def trigger_scan(self):
                _FakeNAXApi.scan_triggered = True

        ssh = FakeSSHClient(command_results={"docker restart": (0, "restarted", "")})
        api = _FakeNAXApi()
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            netalertx_api_client=api,
        )
        result = asyncio.run(executor.execute(ToolCall(name="restart_netalertx")))
        assert result.success
        assert _FakeNAXApi.scan_triggered

    def test_restart_netalertx_ssh_exception(self):
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        class _RaisingSSH(FakeSSHClient):
            async def run(self, command, check=False):
                raise OSError("SSH failed")

        executor = self._make_executor(ssh=_RaisingSSH())
        result = asyncio.run(executor.execute(ToolCall(name="restart_netalertx")))
        assert not result.success
        assert "SSH failed" in result.error

    # -- rewrite_netalertx_conf --------------------------------------------------

    def test_rewrite_netalertx_conf_success(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        # All required keys present so validator passes
        initial_conf = (
            "MQTT_BROKER='homeassistant.local'\n"
            "MQTT_PORT=1883\n"
            "HA_URL='http://homeassistant.local:8123'\n"
            "HA_BEARER_TOKEN='mytoken'\n"
            "SCAN_SUBNETS=['192.168.1.0/24']\n"
            "TIMEZONE='UTC'\n"
            "LOADED_PLUGINS='MQTT,ARPSCAN'\n"
            "MQTT_ACTIVE=0\n"
        )
        ssh = FakeSSHClient(file_contents={"/data/app.conf": initial_conf})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="rewrite_netalertx_conf",
                    arguments={"overrides": {"MQTT_ACTIVE": "1"}},
                )
            )
        )
        assert result.success
        assert "updated" in result.output

    def test_rewrite_netalertx_conf_validation_blocks_bad_overrides(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        # Missing TIMEZONE and LOADED_PLUGINS — validator will block
        initial_conf = "SCAN_SUBNETS=[]\nMQTT_ACTIVE=0\n"
        ssh = FakeSSHClient(file_contents={"/data/app.conf": initial_conf})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="rewrite_netalertx_conf",
                    arguments={"overrides": {"SCAN_SUBNETS": "[]"}},
                )
            )
        )
        assert not result.success
        assert "invalid" in result.error.lower()

    # -- unknown tool and top-level exception ------------------------------------

    def test_unknown_tool_name(self):
        from utils.tool_registry import ToolCall

        executor = self._make_executor()
        result = asyncio.run(executor.execute(ToolCall(name="does_not_exist_at_all")))
        assert not result.success
        assert "Unknown tool" in result.error

    def test_execute_catches_unexpected_exception(self):
        from utils.tool_registry import ToolCall

        class _BrokenSSH:
            async def read_file(self, path):
                raise RuntimeError("Unexpected critical error")

            async def write_file(self, path, content):
                pass

            async def download_file(self, remote_path, local_path):
                pass

            async def run(self, command, check=False):
                raise RuntimeError("Unexpected critical error")

            def stream_lines(self, command):
                return iter([])

        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.tool_executor import ToolExecutor

        executor = ToolExecutor(
            ha_ssh_client=_BrokenSSH(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="read_config", arguments={"path": "/config/configuration.yaml"}
                )
            )
        )
        assert not result.success

    # -- apply_fix backup failure ------------------------------------------------

    def test_apply_fix_backup_failure(self, monkeypatch):
        """apply_fix returns error when execute_remote_backup raises."""
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        executor = self._make_executor(ssh=ssh)

        # Make queue_for_approval return True (auto-execute) so the backup runs
        async def _always_approve(*args, **kwargs):
            return True

        monkeypatch.setattr(executor._gate, "queue_for_approval", _always_approve)

        # Make backup fail
        async def _fail_backup(*args, **kwargs):
            raise RuntimeError("backup storage full")

        monkeypatch.setattr(ha_agent_advanced, "execute_remote_backup", _fail_backup)

        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="apply_fix",
                    arguments={
                        "yaml_content": "homeassistant:\n  name: Home\n",
                        "description": "fix",
                    },
                )
            )
        )
        assert not result.success
        assert "Backup failed" in result.error

    # -- restart_netalertx scan trigger exception (best-effort) -----------------

    def test_restart_netalertx_scan_trigger_exception_is_swallowed(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _FailingScanApi:
            async def get_about(self):
                return {}

            async def get_devices(self):
                return []

            async def get_events(self):
                return []

            async def trigger_scan(self):
                raise RuntimeError("scan endpoint unavailable")

        ssh = FakeSSHClient(command_results={"docker restart": (0, "restarted", "")})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            netalertx_api_client=_FailingScanApi(),
        )
        # scan trigger raises but is best-effort — overall result should still succeed
        result = asyncio.run(executor.execute(ToolCall(name="restart_netalertx")))
        assert result.success

    # -- rewrite_netalertx_conf with no existing config file --------------------

    def test_rewrite_netalertx_conf_missing_file_starts_fresh(self):
        """If app.conf doesn't exist, _rewrite_netalertx_conf starts from empty."""
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        # FakeSSHClient with no file_contents raises FileNotFoundError
        ssh = FakeSSHClient()
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
        )
        # Result may succeed or fail depending on whether the empty config passes
        # validation — the key invariant is that no exception propagates.
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="rewrite_netalertx_conf",
                    arguments={"overrides": {"SCAN_SUBNETS": "['192.168.1.0/24']"}},
                )
            )
        )
        assert isinstance(result.success, bool)

    def test_rewrite_netalertx_conf_write_failure(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _FailWrite(FakeSSHClient):
            async def write_file(self, path, content):
                raise OSError("disk full")

        initial_conf = (
            "MQTT_BROKER='homeassistant.local'\n"
            "MQTT_PORT=1883\n"
            "HA_URL='http://homeassistant.local:8123'\n"
            "HA_BEARER_TOKEN='mytoken'\n"
            "SCAN_SUBNETS=['192.168.1.0/24']\n"
            "TIMEZONE='UTC'\n"
            "LOADED_PLUGINS='MQTT,ARPSCAN'\n"
        )
        ssh = _FailWrite(file_contents={"/data/app.conf": initial_conf})
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="rewrite_netalertx_conf",
                    arguments={"overrides": {"MQTT_ACTIVE": "1"}},
                )
            )
        )
        assert not result.success
        assert "disk full" in result.error

    # -- execute() outer exception handler ---------------------------------------

    def test_execute_outer_exception_handler(self):
        """An exception escaping a tool method is caught by execute()'s outer handler."""
        from utils.autonomy import FakeAutonomyGate
        from utils.knowledge_store import FakeKnowledgeStore
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        class _RaisingStore(FakeKnowledgeStore):
            def query(self, query_text, top_k, collections=None):
                raise RuntimeError("store exploded")

        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            knowledge_store=_RaisingStore(),
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="query_knowledge", arguments={"query": "foo"})
            )
        )
        assert not result.success

    # -- apply_fix with read failure on original config --------------------------

    def test_apply_fix_original_config_read_failure(self):
        from utils.tool_registry import ToolCall

        # FakeSSHClient with no file_contents raises FileNotFoundError for any path
        executor = self._make_executor()
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="apply_fix",
                    arguments={"yaml_content": "key: value", "description": "test fix"},
                )
            )
        )
        assert not result.success
        assert "Could not read original config" in result.error

    # -- query_knowledge ---------------------------------------------------------

    def test_query_knowledge_with_store_returns_results(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.knowledge_store import FakeKnowledgeStore
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-2024.1-0"],
            documents=["Template syntax breaking change in 2024.1"],
            metadatas=[{"source": "ha_release_notes/2024.1"}],
        )
        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            knowledge_store=store,
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="query_knowledge", arguments={"query": "template syntax"})
            )
        )
        assert result.success
        assert "Template syntax" in result.output

    # -- apply_fix HITL non-blocking queuing path --------------------------------

    def test_apply_fix_hitl_queuing_returns_awaiting_approval(self):
        """When gate.queue_for_approval returns False, apply_fix returns awaiting_approval."""
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        gate = FakeAutonomyGate(auto_execute_result=False, approval_result=True)
        notifier = FakeNotifier(approve=True)
        executor = ToolExecutor(ha_ssh_client=ssh, gate=gate, notifier=notifier)

        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="apply_fix",
                    arguments={
                        "yaml_content": "homeassistant:\n  name: Home\n",
                        "description": "add missing key",
                    },
                )
            )
        )
        assert result.awaiting_approval is True
        assert not result.success
        # HITL notification should have been sent with the fix embedded
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert "pending_fix_yaml" in payload
        assert payload["pending_fix_yaml"] == "homeassistant:\n  name: Home\n"

    def test_apply_fix_hitl_queuing_sets_apply_fix_used_flag(self):
        """Queuing for HITL sets _apply_fix_used so double-queuing is blocked."""
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        ssh = FakeSSHClient(
            file_contents={
                "/config/configuration.yaml": "homeassistant:\n  name: Home\n"
            }
        )
        executor = ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=False),
            notifier=FakeNotifier(approve=True),
        )
        asyncio.run(
            executor.execute(
                ToolCall(
                    name="apply_fix",
                    arguments={
                        "yaml_content": "homeassistant:\n  name: Home\n",
                        "description": "fix",
                    },
                )
            )
        )
        assert executor._apply_fix_used

    def test_fake_autonomy_gate_queue_for_approval_auto_execute(self):
        """FakeAutonomyGate.queue_for_approval returns True when auto_execute_result=True."""
        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=True)
        notifier = FakeNotifier()
        result = asyncio.run(
            gate.queue_for_approval(
                subject="test",
                body="body",
                payload={},
                notifier=notifier,
                risk=RiskLevel.HIGH,
            )
        )
        assert result is True
        assert len(notifier.sent) == 0

    def test_fake_autonomy_gate_queue_for_approval_sends_notification(self):
        """FakeAutonomyGate.queue_for_approval sends notification and returns False when not auto."""
        from utils.autonomy import FakeAutonomyGate, RiskLevel
        from utils.notify import FakeNotifier

        gate = FakeAutonomyGate(auto_execute_result=False)
        notifier = FakeNotifier()
        result = asyncio.run(
            gate.queue_for_approval(
                subject="test",
                body="body",
                payload={"notification_id": "abc"},
                notifier=notifier,
                risk=RiskLevel.HIGH,
            )
        )
        assert result is False
        assert len(notifier.sent) == 1

    def test_query_knowledge_with_store_no_results(self):
        from utils.autonomy import FakeAutonomyGate
        from utils.knowledge_store import FakeKnowledgeStore
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolCall

        store = FakeKnowledgeStore()
        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
            knowledge_store=store,
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="query_knowledge", arguments={"query": "something obscure"}
                )
            )
        )
        assert result.success
        assert "No relevant knowledge" in result.output


# ── AgentLoop — item 44 ─────────────────────────────────────────────────────────


class TestAgentLoop:
    def _make_loop(self, call_sequence, ssh=None, gate=None, notifier=None):
        from utils.agent_loop import AgentLoop
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolRegistry, ToolDefinition

        reg = ToolRegistry()
        for name in ("read_config", "finish_repair", "apply_fix"):
            reg.register(
                ToolDefinition(
                    name=name,
                    description=f"{name} tool",
                    parameters={"type": "object", "properties": {}, "required": []},
                )
            )

        executor = ToolExecutor(
            ha_ssh_client=ssh or FakeSSHClient(),
            gate=gate
            or FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=notifier or FakeNotifier(approve=True),
        )
        return AgentLoop(
            llm_client=FakeToolCallingLLMClient(call_sequence),
            tool_executor=executor,
            tool_registry=reg,
            max_tool_calls=5,
            max_wall_seconds=30.0,
        )

    def test_finish_repair_returns_success(self):
        loop = self._make_loop(
            call_sequence=[
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "config is fine",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                }
            ]
        )
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "success"
        assert result.episode_stub is not None
        assert result.episode_stub["action_taken"] == "no_fix_needed"

    def test_no_tool_calls_returns_exhausted(self):
        # Two consecutive plain-text responses trigger exhausted after the
        # recovery nudge on the first one.
        loop = self._make_loop(
            call_sequence=[
                {"content": "I cannot help with this"},
                {"content": "Still no tool call"},
            ]
        )
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "exhausted"

    def test_no_tool_calls_captures_last_plain_text_as_summary(self):
        # When the loop exhausts due to plain-text responses, the last
        # plain-text content is captured in episode_stub["summary"] so the
        # chat UI can surface it instead of "(Loop ended: exhausted)".
        loop = self._make_loop(
            call_sequence=[
                {"content": "Yes, I can help with Home Assistant."},
                {"content": "Here are the details about my capabilities."},
            ]
        )
        result = asyncio.run(loop.run("Are you capable of anything?"))
        assert result.outcome == "exhausted"
        assert result.episode_stub is not None
        assert (
            result.episode_stub["summary"]
            == "Here are the details about my capabilities."
        )

    def test_single_plain_text_then_finish_repair_succeeds(self):
        # One plain-text response triggers the recovery nudge; the model then
        # calls finish_repair and the loop ends with 'success'.
        from utils.ollama_client import FakeToolCallingLLMClient

        call_sequence = [
            {"content": "Let me think about this."},
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "finish_repair",
                            "arguments": {
                                "summary": "No issues found",
                                "action_taken": "no_fix_needed",
                            },
                        }
                    }
                ]
            },
        ]
        loop = self._make_loop(call_sequence=call_sequence)
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "success"
        assert len(result.steps) == 1  # only finish_repair counts as a step

    def test_budget_exhaustion(self):
        # 6 read_config calls but max_tool_calls=5 → exhausted after 5
        call_sequence = [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_config",
                            "arguments": {"path": "/config/configuration.yaml"},
                        }
                    }
                ]
            }
        ] * 6
        loop = self._make_loop(call_sequence=call_sequence)
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "exhausted"
        assert len(result.steps) == 5

    def test_steps_are_recorded(self):
        loop = self._make_loop(
            call_sequence=[
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_config",
                                "arguments": {"path": "/config/configuration.yaml"},
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "ok",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                },
            ]
        )
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "success"
        assert len(result.steps) == 2
        assert result.steps[0].tool_call.name == "read_config"
        assert result.steps[1].tool_call.name == "finish_repair"

    def test_step_numbers_are_sequential(self):
        loop = self._make_loop(
            call_sequence=[
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_config",
                                "arguments": {"path": "/config/configuration.yaml"},
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "done",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                },
            ]
        )
        result = asyncio.run(loop.run("Check config"))
        assert result.steps[0].step_number == 1
        assert result.steps[1].step_number == 2

    def test_empty_sequence_returns_exhausted(self):
        loop = self._make_loop(call_sequence=[])
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "exhausted"

    # ── content-fallback tests ────────────────────────────────────────────

    def test_parse_content_as_tool_call_valid(self):
        """JSON content matching a known tool name is parsed into tool_calls."""
        from utils.agent_loop import _parse_content_as_tool_call

        known = {"read_config", "finish_repair"}
        content = '{"name": "read_config", "arguments": {"path": "/config/configuration.yaml"}}'
        result = _parse_content_as_tool_call(content, known)
        assert result is not None
        assert result[0]["function"]["name"] == "read_config"
        assert result[0]["function"]["arguments"] == {
            "path": "/config/configuration.yaml"
        }

    def test_parse_content_as_tool_call_unknown_tool_returns_none(self):
        """Tool name not in known set is ignored (avoids false positives)."""
        from utils.agent_loop import _parse_content_as_tool_call

        known = {"read_config"}
        content = '{"name": "rm_rf", "arguments": {}}'
        assert _parse_content_as_tool_call(content, known) is None

    def test_parse_content_as_tool_call_plain_text_returns_none(self):
        """Ordinary text content is not mis-parsed as a tool call."""
        from utils.agent_loop import _parse_content_as_tool_call

        known = {"read_config", "finish_repair"}
        assert _parse_content_as_tool_call("Config looks fine.", known) is None
        assert _parse_content_as_tool_call("", known) is None
        assert _parse_content_as_tool_call("{bad json}", known) is None

    def test_content_fallback_dispatches_tool_call(self):
        """Loop executes a tool call embedded in message content (qwen quirk)."""
        from utils.ollama_client import FakeToolCallingLLMClient

        # First response: tool call in content field, no tool_calls key.
        # Second response: proper finish_repair tool_calls.
        content_call = {
            "content": '{"name": "read_config", "arguments": {"path": "/config/configuration.yaml"}}'
        }
        finish_call = {
            "tool_calls": [
                {
                    "function": {
                        "name": "finish_repair",
                        "arguments": {
                            "summary": "all good",
                            "action_taken": "no_fix_needed",
                        },
                    }
                }
            ]
        }
        loop = self._make_loop(call_sequence=[content_call, finish_call])
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "success"
        assert len(result.steps) == 2
        assert result.steps[0].tool_call.name == "read_config"
        assert result.steps[1].tool_call.name == "finish_repair"

    def test_executor_reset_called_on_run(self):
        """AgentLoop.run() must reset the executor so apply_fix cap resets."""
        from utils.agent_loop import AgentLoop
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolDefinition, ToolRegistry

        reg = ToolRegistry()
        reg.register(
            ToolDefinition(
                name="finish_repair",
                description="done",
                parameters={"type": "object", "properties": {}, "required": []},
            )
        )
        executor = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(approve=True),
        )
        executor._apply_fix_used = True  # simulate stale state from prior run

        loop = AgentLoop(
            llm_client=FakeToolCallingLLMClient(
                [
                    {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "finish_repair",
                                    "arguments": {
                                        "summary": "ok",
                                        "action_taken": "no_fix_needed",
                                    },
                                }
                            }
                        ]
                    }
                ]
            ),
            tool_executor=executor,
            tool_registry=reg,
        )
        asyncio.run(loop.run("context"))
        # After reset, the flag should have been cleared at the start of the run.
        # We verify indirectly: the loop completed successfully (no cap error).
        assert not executor._apply_fix_used  # reset was called

    def test_awaiting_approval_result_exits_loop(self):
        """AgentLoop exits with 'awaiting_approval' when a tool returns awaiting_approval=True."""
        from utils.agent_loop import AgentLoop
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import (
            ToolCall,
            ToolDefinition,
            ToolRegistry,
            ToolResult,
        )

        reg = ToolRegistry()
        for name in ("apply_fix", "finish_repair"):
            reg.register(
                ToolDefinition(
                    name=name,
                    description=f"{name} tool",
                    parameters={"type": "object", "properties": {}, "required": []},
                )
            )

        class _HITLExecutor(ToolExecutor):
            async def execute(self, tool_call: ToolCall) -> ToolResult:
                if tool_call.name == "apply_fix":
                    return ToolResult(
                        tool_name="apply_fix",
                        success=False,
                        output="queued",
                        awaiting_approval=True,
                    )
                return await super().execute(tool_call)

        executor = _HITLExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(auto_execute_result=True),
            notifier=FakeNotifier(approve=True),
        )
        loop = AgentLoop(
            llm_client=FakeToolCallingLLMClient(
                [{"tool_calls": [{"function": {"name": "apply_fix", "arguments": {}}}]}]
            ),
            tool_executor=executor,
            tool_registry=reg,
        )
        result = asyncio.run(loop.run("Check config"))
        assert result.outcome == "awaiting_approval"
        assert len(result.steps) == 1

    def test_continuation_nudge_injected_after_tool_result(self):
        """A 'Continue' user message is injected into history after each tool result."""
        from utils.agent_loop import AgentLoop
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolDefinition, ToolRegistry

        reg = ToolRegistry()
        for name in ("read_config", "finish_repair"):
            reg.register(
                ToolDefinition(
                    name=name,
                    description=f"{name} tool",
                    parameters={"type": "object", "properties": {}, "required": []},
                )
            )
        client = FakeToolCallingLLMClient(
            call_sequence=[
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_config",
                                "arguments": {"path": "/config/configuration.yaml"},
                            }
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "ok",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                },
            ]
        )
        loop = AgentLoop(
            llm_client=client,
            tool_executor=ToolExecutor(
                ha_ssh_client=FakeSSHClient(),
                gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
                notifier=FakeNotifier(approve=True),
            ),
            tool_registry=reg,
            max_tool_calls=5,
        )
        asyncio.run(loop.run("Check config"))

        # FakeToolCallingLLMClient stores a reference (not a snapshot) to the messages
        # list, so calls[0]["messages"] reflects the final state after the run.
        # Verify a 'Continue' user nudge was injected somewhere in the history.
        all_messages = client.calls[0]["messages"]
        nudges = [
            m
            for m in all_messages
            if m.get("role") == "user" and "Continue" in m.get("content", "")
        ]
        assert len(nudges) == 1

    def test_content_fallback_clears_content_field(self):
        """When content fallback fires, the assistant message stored in history has content=''."""
        from utils.agent_loop import AgentLoop
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ollama_client import FakeToolCallingLLMClient
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor
        from utils.tool_registry import ToolDefinition, ToolRegistry

        reg = ToolRegistry()
        for name in ("read_config", "finish_repair"):
            reg.register(
                ToolDefinition(
                    name=name,
                    description=f"{name} tool",
                    parameters={"type": "object", "properties": {}, "required": []},
                )
            )
        client = FakeToolCallingLLMClient(
            call_sequence=[
                # Turn 1: tool call embedded in content (qwen2.5-coder quirk)
                {
                    "content": '{"name": "read_config", "arguments": {"path": "/config/configuration.yaml"}}'
                },
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "finish_repair",
                                "arguments": {
                                    "summary": "ok",
                                    "action_taken": "no_fix_needed",
                                },
                            }
                        }
                    ]
                },
            ]
        )
        loop = AgentLoop(
            llm_client=client,
            tool_executor=ToolExecutor(
                ha_ssh_client=FakeSSHClient(),
                gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
                notifier=FakeNotifier(approve=True),
            ),
            tool_registry=reg,
            max_tool_calls=5,
        )
        asyncio.run(loop.run("Check config"))

        # FakeToolCallingLLMClient stores a reference (not a snapshot) to the messages
        # list, so calls[0]["messages"] reflects the final state after the run.
        # Find the assistant message produced by the content-fallback path (read_config)
        # and verify its content field was cleared.
        all_messages = client.calls[0]["messages"]
        read_config_assistants = [
            m
            for m in all_messages
            if m.get("role") == "assistant"
            and m.get("tool_calls")
            and m["tool_calls"][0]["function"]["name"] == "read_config"
        ]
        assert len(read_config_assistants) == 1
        assert read_config_assistants[0].get("content") == ""

    def test_system_prompt_requires_finish_repair(self):
        """Default system prompt explicitly mandates finish_repair and prohibits plain text."""
        from utils.agent_loop import _AGENT_LOOP_SYSTEM_PROMPT

        prompt_lower = _AGENT_LOOP_SYSTEM_PROMPT.lower()
        assert "finish_repair" in _AGENT_LOOP_SYSTEM_PROMPT
        assert "mandatory" in prompt_lower or "required" in prompt_lower
        assert "plain text" in prompt_lower or "never return" in prompt_lower


class TestRunRagRefresh:
    """Tests for run_rag_refresh() — all network-dependent functions are mocked."""

    def _patch_network(self, monkeypatch, tmp_path):
        """Patch all network-dependent functions to no-ops."""
        import utils.ha_docs_scraper as docs_mod
        import utils.ha_release_notes_scraper as ha_mod
        import utils.hacs_scraper as hacs_mod

        monkeypatch.setattr(ha_mod, "fetch_ha_release_notes", lambda *a, **kw: 0)
        monkeypatch.setattr(hacs_mod, "discover_hacs_integrations", lambda *a, **kw: [])
        monkeypatch.setattr(hacs_mod, "fetch_hacs_changelog", lambda *a, **kw: None)
        monkeypatch.setattr(
            docs_mod, "discover_installed_integrations", lambda *a, **kw: []
        )
        monkeypatch.setattr(docs_mod, "fetch_integration_doc", lambda *a, **kw: -1)
        monkeypatch.setattr("config.HA_API_TOKEN", "test-token")
        monkeypatch.setattr(
            "config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR", str(tmp_path / "ha_notes")
        )
        monkeypatch.setattr("config.RAG_HACS_CACHE_DIR", str(tmp_path / "hacs"))
        monkeypatch.setattr("config.RAG_HA_DOCS_CACHE_DIR", str(tmp_path / "docs"))

    def test_produces_progress_output(self, tmp_path, monkeypatch, capsys):
        from utils.knowledge_store import FakeKnowledgeStore
        import main as main_module

        self._patch_network(monkeypatch, tmp_path)
        store = FakeKnowledgeStore()
        main_module.run_rag_refresh(store)
        out = capsys.readouterr().out
        assert "rag-refresh" in out
        assert "HA release note" in out
        assert "HACS" in out
        assert "integration doc" in out

    def test_embeds_ha_release_notes(self, tmp_path, monkeypatch):
        from utils.knowledge_store import FakeKnowledgeStore
        import main as main_module

        notes_dir = tmp_path / "ha_notes"
        notes_dir.mkdir()
        (notes_dir / "2024.1.txt").write_text(
            "# Home Assistant 2024.1\n## Breaking changes\nRemoved old_key from config."
            "\n## New features\nAdded new light platform."
        )
        self._patch_network(monkeypatch, tmp_path)
        monkeypatch.setattr("config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR", str(notes_dir))
        store = FakeKnowledgeStore()
        main_module.run_rag_refresh(store)
        # breaking-change content
        assert len(store.query("removed old_key", top_k=5)) > 0
        assert (
            store.query("removed old_key", top_k=5)[0].collection == "ha_release_notes"
        )
        # new feature content also embedded
        assert len(store.query("new light platform", top_k=5)) > 0

    def test_embeds_hacs_changelogs(self, tmp_path, monkeypatch):
        from utils.knowledge_store import FakeKnowledgeStore
        import main as main_module

        hacs_dir = tmp_path / "hacs"
        hacs_dir.mkdir()
        (hacs_dir / "myintegration.md").write_text(
            "## 1.2.0\nAdded new feature.\n## 1.1.0\nFixed bug."
        )
        self._patch_network(monkeypatch, tmp_path)
        monkeypatch.setattr("config.RAG_HACS_CACHE_DIR", str(hacs_dir))
        store = FakeKnowledgeStore()
        main_module.run_rag_refresh(store)
        results = store.query("new feature", top_k=5)
        assert len(results) > 0
        assert results[0].collection == "hacs_changelogs"

    def test_embeds_integration_docs(self, tmp_path, monkeypatch):
        from utils.knowledge_store import FakeKnowledgeStore
        import main as main_module

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "hue.md").write_text(
            "## Overview\nPhilips Hue integration.\n## Configuration\nAdd API token."
        )
        self._patch_network(monkeypatch, tmp_path)
        monkeypatch.setattr("config.RAG_HA_DOCS_CACHE_DIR", str(docs_dir))
        store = FakeKnowledgeStore()
        main_module.run_rag_refresh(store)
        results = store.query("Philips Hue", top_k=5)
        assert len(results) > 0
        assert results[0].collection == "ha_integration_docs"

    def test_skips_hacs_discovery_when_no_token(self, tmp_path, monkeypatch, capsys):
        from utils.knowledge_store import FakeKnowledgeStore
        import main as main_module

        self._patch_network(monkeypatch, tmp_path)
        monkeypatch.setattr("config.HA_API_TOKEN", "")
        store = FakeKnowledgeStore()
        main_module.run_rag_refresh(store)
        out = capsys.readouterr().out
        assert "no HA API token" in out


class TestLoopCrashTimeline:
    """LoopSupervisor writes a timeline event when a loop crashes."""

    def test_crash_writes_loop_crash_timeline_event(self, tmp_path):
        import sqlite3 as _sq
        import utils.timeline
        from utils.supervisor import LoopSupervisor

        # The _patch_timeline_db autouse fixture already redirected utils.timeline.DB_PATH
        # to a per-test temp DB with the timeline_events table.
        call_count = 0

        async def _crashing_factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            # Second iteration: return cleanly so the loop exits
            return

        async def _run():
            sv = LoopSupervisor(backoff_start=0.0, backoff_cap=0.0)
            sv.start("test_loop", _crashing_factory)
            # Give the event loop a few ticks to run the loop and restart it
            for _ in range(20):
                await asyncio.sleep(0)

        asyncio.run(_run())

        rows = (
            _sq.connect(utils.timeline.DB_PATH)
            .execute(
                "SELECT level, source, message, detail_json FROM timeline_events"
                " WHERE source = 'loop_crash'"
            )
            .fetchall()
        )
        assert rows, "expected a loop_crash timeline event"
        import json

        detail = json.loads(rows[0][3])
        assert detail["loop"] == "test_loop"
        assert "boom" in detail["error"]


# ── Phase 12.5 — HA Repairs + Update Ordering ─────────────────────────────────


class TestHARepairIssue:
    def test_dataclass_construction(self):
        from utils.ha_rest_client import HARepairIssue

        issue = HARepairIssue(
            domain="hassio",
            issue_id="abc123",
            severity="warning",
            issue_key="hassio/abc123",
            breaks_in_ha_version=None,
            translation_key="issue_system_reboot_required",
            data={"foo": "bar"},
        )
        assert issue.domain == "hassio"
        assert issue.issue_key == "hassio/abc123"
        assert issue.translation_key == "issue_system_reboot_required"

    def test_dataclass_defaults(self):
        from utils.ha_rest_client import HARepairIssue

        issue = HARepairIssue(
            domain="d",
            issue_id="i",
            severity="warning",
            issue_key="d/i",
            breaks_in_ha_version=None,
        )
        assert issue.data == {}
        assert issue.breaks_in_ha_version is None
        assert issue.translation_key is None

    def test_get_ha_repair_issues_parses_response(self):
        from utils.ha_rest_client import get_ha_repair_issues
        from utils.ha_ws_client import FakeHAWebSocketClient

        client = FakeHAWebSocketClient(
            repair_issues=[
                {
                    "domain": "hassio",
                    "issue_id": "abc123",
                    "severity": "warning",
                    "breaks_in_ha_version": None,
                    "translation_key": "issue_system_reboot_required",
                    "ignored": False,
                },
                {
                    "domain": "zha",
                    "issue_id": "missing_config",
                    "severity": "warning",
                    "breaks_in_ha_version": "2026.9.0",
                    "translation_key": "missing_config_entry",
                    "ignored": False,
                },
            ]
        )
        issues = asyncio.run(get_ha_repair_issues(client))
        assert len(issues) == 2
        assert issues[0].issue_key == "hassio/abc123"
        assert issues[0].translation_key == "issue_system_reboot_required"
        assert issues[1].breaks_in_ha_version == "2026.9.0"

    def test_get_ha_repair_issues_skips_ignored(self):
        from utils.ha_rest_client import get_ha_repair_issues
        from utils.ha_ws_client import FakeHAWebSocketClient

        client = FakeHAWebSocketClient(
            repair_issues=[
                {
                    "domain": "hassio",
                    "issue_id": "abc123",
                    "severity": "warning",
                    "ignored": True,
                    "translation_key": "something",
                },
            ]
        )
        issues = asyncio.run(get_ha_repair_issues(client))
        assert issues == []

    def test_get_ha_repair_issues_propagates_ws_error(self):
        """WebSocket errors propagate so callers can apply their own retry/backoff logic."""
        from utils.ha_rest_client import get_ha_repair_issues
        from utils.ha_ws_client import FakeHAWebSocketClient

        class ErrorClient(FakeHAWebSocketClient):
            async def get_repair_issues(self) -> list[dict]:
                raise RuntimeError("connection refused")

        with pytest.raises(RuntimeError, match="connection refused"):
            asyncio.run(get_ha_repair_issues(ErrorClient()))

    def test_dismiss_ha_repair_issue_calls_delete(self):
        from utils.ha_rest_client import FakeHARestClient, dismiss_ha_repair_issue

        client = FakeHARestClient()
        asyncio.run(dismiss_ha_repair_issue(client, "homeassistant", "reboot_required"))
        assert "/api/repairs/issues/homeassistant/reboot_required" in client.deleted


class TestHARepairDB:
    @pytest.fixture
    def db_path(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        db = tmp_path / "test.db"
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", str(db))
        ha_agent_advanced.init_local_database()
        return str(db)

    def test_record_repair_seen_first_time_returns_true(self, db_path):
        from ha_agent_advanced import record_repair_seen

        is_new = record_repair_seen("homeassistant/reboot_required")
        assert is_new is True

    def test_record_repair_seen_second_time_returns_false(self, db_path):
        from ha_agent_advanced import record_repair_seen

        record_repair_seen("homeassistant/reboot_required")
        is_new = record_repair_seen("homeassistant/reboot_required")
        assert is_new is False

    def test_mark_repair_hitl_sent_sets_timestamp(self, db_path):
        import sqlite3

        from ha_agent_advanced import mark_repair_hitl_sent, record_repair_seen

        record_repair_seen("d/i")
        mark_repair_hitl_sent("d/i")
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT hitl_sent_at FROM ha_repair_history WHERE issue_key='d/i'"
            ).fetchone()
        assert row is not None and row[0] is not None

    def test_mark_repair_resolved_sets_timestamp(self, db_path):
        import sqlite3

        from ha_agent_advanced import mark_repair_resolved, record_repair_seen

        record_repair_seen("d/i")
        mark_repair_resolved("d/i")
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT resolved_at FROM ha_repair_history WHERE issue_key='d/i'"
            ).fetchone()
        assert row is not None and row[0] is not None

    def test_is_reboot_required_active_false_when_no_row(self, db_path):
        from ha_agent_advanced import is_reboot_required_active

        assert is_reboot_required_active() is False

    def test_is_reboot_required_active_false_before_hitl_sent(self, db_path):
        from ha_agent_advanced import is_reboot_required_active, record_repair_seen

        record_repair_seen(
            "hassio/abc123", translation_key="issue_system_reboot_required"
        )
        assert is_reboot_required_active() is False

    def test_is_reboot_required_active_true_after_hitl_sent(self, db_path):
        from ha_agent_advanced import (
            is_reboot_required_active,
            mark_repair_hitl_sent,
            record_repair_seen,
        )

        record_repair_seen(
            "hassio/abc123", translation_key="issue_system_reboot_required"
        )
        mark_repair_hitl_sent("hassio/abc123")
        assert is_reboot_required_active() is True

    def test_is_reboot_required_active_false_after_resolved(self, db_path):
        from ha_agent_advanced import (
            is_reboot_required_active,
            mark_repair_hitl_sent,
            mark_repair_resolved,
            record_repair_seen,
        )

        record_repair_seen(
            "hassio/abc123", translation_key="issue_system_reboot_required"
        )
        mark_repair_hitl_sent("hassio/abc123")
        mark_repair_resolved("hassio/abc123")
        assert is_reboot_required_active() is False

    def test_is_reboot_required_active_false_without_translation_key(self, db_path):
        """A row with no translation_key does not trigger is_reboot_required_active."""
        from ha_agent_advanced import (
            is_reboot_required_active,
            mark_repair_hitl_sent,
            record_repair_seen,
        )

        record_repair_seen("hassio/abc123")  # no translation_key
        mark_repair_hitl_sent("hassio/abc123")
        assert is_reboot_required_active() is False


class TestUpdatePriority:
    def test_os_is_highest_priority(self):
        from ha_update_manager import _update_priority

        assert _update_priority("os") == 0

    def test_supervisor_priority(self):
        from ha_update_manager import _update_priority

        assert _update_priority("supervisor") == 1

    def test_core_priority(self):
        from ha_update_manager import _update_priority

        assert _update_priority("core") == 2

    def test_addon_fallback_priority(self):
        from ha_update_manager import _update_priority

        assert _update_priority("my_addon") == 3

    def test_pending_higher_returns_empty_when_no_others(self, tmp_path):
        from ha_update_manager import _pending_higher_priority_components

        result = _pending_higher_priority_components("core", tmp_path)
        assert result == []

    def test_pending_higher_detects_os_when_approving_core(self, tmp_path):
        import json

        from ha_update_manager import _pending_higher_priority_components
        from utils.card_types import CARD_TYPE_UPDATE

        card = tmp_path / "abc.json"
        card.write_text(
            json.dumps({"payload": {"card_type": CARD_TYPE_UPDATE, "component": "os"}})
        )
        result = _pending_higher_priority_components("core", tmp_path)
        assert "os" in result

    def test_pending_higher_ignores_approved_card(self, tmp_path):
        import json

        from ha_update_manager import _pending_higher_priority_components
        from utils.card_types import CARD_TYPE_UPDATE

        card = tmp_path / "abc.json"
        card.write_text(
            json.dumps({"payload": {"card_type": CARD_TYPE_UPDATE, "component": "os"}})
        )
        (tmp_path / "abc.approved").touch()
        result = _pending_higher_priority_components("core", tmp_path)
        assert result == []


class TestExecuteUpdatePreflight:
    def _make_update(self, component: str = "core"):
        from utils.ha_rest_client import UpdateStatus

        return UpdateStatus(
            component=component,
            entity_id=f"update.{component}",
            installed_version="1.0",
            latest_version="2.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

    def test_blocked_when_reboot_required_active(self):
        from ha_update_manager import execute_update
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from unittest.mock import patch

        ssh = FakeSSHClient()
        gate = FakeAutonomyGate()
        notifier = FakeNotifier()

        with patch(
            "ha_agent_advanced.is_reboot_required_active", return_value=True
        ), patch("ha_update_manager.execute_core_update") as mock_exec:
            result = asyncio.run(
                execute_update(self._make_update("core"), ssh, notifier, gate)
            )

        assert result is False
        mock_exec.assert_not_called()
        assert len(notifier.sent) == 1
        assert "reboot" in notifier.sent[0]["body"].lower()


# ── poll_for_notifications backoff ────────────────────────────────────────────
class TestNotificationPollBackoff:
    @pytest.fixture
    def db_path(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_backoff_grows_on_consecutive_failures(self, db_path, monkeypatch):
        """Sleep interval grows 30 → 60 → 120 after consecutive failures, resets on success."""
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_notifications
        from utils.notify import FakeNotifier

        sleep_calls: list[float] = []
        iteration = [0]

        async def tracking_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            iteration[0] += 1
            if iteration[0] >= 5:
                raise asyncio.CancelledError()

        class FlakyWS:
            attempt = 0

            async def get_persistent_notifications(self) -> list:
                FlakyWS.attempt += 1
                if FlakyWS.attempt <= 3:
                    raise RuntimeError("connection refused")
                return []  # success on 4th attempt

            async def get_device_registry(self) -> list:
                return []

        monkeypatch.setattr(asyncio_mod, "sleep", tracking_sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=FlakyWS(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )

        # Verify the backoff sequence: normal-interval, 30, 60, 120, then normal-interval again.
        assert len(sleep_calls) == 5
        assert sleep_calls[1] == 30
        assert sleep_calls[2] == 60
        assert sleep_calls[3] == 120
        assert sleep_calls[4] == sleep_calls[0]  # reset to normal interval

    def test_backoff_caps_at_120(self, db_path, monkeypatch):
        """After the third failure the backoff stays at 120 — it does not grow beyond that."""
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_notifications
        from utils.notify import FakeNotifier

        sleep_calls: list[float] = []
        iteration = [0]

        async def tracking_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            iteration[0] += 1
            if iteration[0] >= 6:
                raise asyncio.CancelledError()

        class AlwaysFailWS:
            async def get_persistent_notifications(self) -> list:
                raise RuntimeError("down")

            async def get_device_registry(self) -> list:
                return []

        monkeypatch.setattr(asyncio_mod, "sleep", tracking_sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_notifications(
                    ha_ws_client=AlwaysFailWS(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )

        # sleep_calls: [interval, 30, 60, 120, 120, 120]
        assert sleep_calls[3] == 120
        assert sleep_calls[4] == 120
        assert sleep_calls[5] == 120


# ── poll_for_repairs backoff ──────────────────────────────────────────────────
class TestRepairPollBackoff:
    @pytest.fixture
    def db_path(self, tmp_path, monkeypatch):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_backoff_grows_on_consecutive_failures(self, db_path, monkeypatch):
        """Sleep interval grows 30 → 60 → 120 after consecutive failures, resets on success."""
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_repairs
        from utils.notify import FakeNotifier

        sleep_calls: list[float] = []
        iteration = [0]

        async def tracking_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            iteration[0] += 1
            if iteration[0] >= 5:
                raise asyncio.CancelledError()

        class FlakyWS:
            attempt = 0

            async def get_repair_issues(self) -> list:
                FlakyWS.attempt += 1
                if FlakyWS.attempt <= 3:
                    raise RuntimeError("connection refused")
                return []  # success on 4th attempt

        monkeypatch.setattr(asyncio_mod, "sleep", tracking_sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_repairs(
                    ha_ws_client=FlakyWS(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )

        # Verify: normal-interval, 30, 60, 120, then normal-interval again.
        assert len(sleep_calls) == 5
        assert sleep_calls[1] == 30
        assert sleep_calls[2] == 60
        assert sleep_calls[3] == 120
        assert sleep_calls[4] == sleep_calls[0]  # reset to normal interval

    def test_backoff_caps_at_120(self, db_path, monkeypatch):
        """Backoff does not grow beyond 120 seconds on sustained failures."""
        import asyncio as asyncio_mod
        from ha_log_monitor import poll_for_repairs
        from utils.notify import FakeNotifier

        sleep_calls: list[float] = []
        iteration = [0]

        async def tracking_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            iteration[0] += 1
            if iteration[0] >= 6:
                raise asyncio.CancelledError()

        class AlwaysFailWS:
            async def get_repair_issues(self) -> list:
                raise RuntimeError("down")

        monkeypatch.setattr(asyncio_mod, "sleep", tracking_sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                poll_for_repairs(
                    ha_ws_client=AlwaysFailWS(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )

        # sleep_calls: [interval, 30, 60, 120, 120, 120]
        assert sleep_calls[3] == 120
        assert sleep_calls[4] == 120
        assert sleep_calls[5] == 120


# ── UpdatePreflight ───────────────────────────────────────────────────────────────


class TestEnforceHaRetentionReturnsCount:
    """enforce_ha_retention() must return an int count of successfully purged slugs."""

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_returns_zero_when_nothing_to_delete(self, db_path):
        import ha_agent_advanced

        ssh = FakeSSHClient()
        result = asyncio.run(ha_agent_advanced.enforce_ha_retention(ssh_client=ssh))
        assert result == 0

    def test_returns_zero_when_below_retention_limit(self, db_path, monkeypatch):
        import ha_agent_advanced

        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 10)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry (backup_slug, timestamp, location) VALUES (?, ?, ?)",
                ("slug-a", 1000, "both"),
            )
        ssh = FakeSSHClient()
        result = asyncio.run(ha_agent_advanced.enforce_ha_retention(ssh_client=ssh))
        assert result == 0

    def test_returns_purge_count_when_slugs_deleted(self, db_path, monkeypatch):
        import ha_agent_advanced

        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 1)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry (backup_slug, timestamp, location) VALUES (?, ?, ?)",
                ("slug-recent", 2000, "both"),
            )
            conn.execute(
                "INSERT INTO backup_registry (backup_slug, timestamp, location) VALUES (?, ?, ?)",
                ("slug-old", 1000, "both"),
            )
        ssh = FakeSSHClient(command_results={"ha backups remove": (0, "", "")})
        result = asyncio.run(ha_agent_advanced.enforce_ha_retention(ssh_client=ssh))
        assert result == 1


_MEMINFO_SAMPLE = "MemAvailable: 524288 kB\nMemTotal: 1048576 kB\n"


class TestUpdatePreflightLogic:
    """Tests for run_update_preflight() in ha_update_manager."""

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import ha_agent_advanced
        import ha_update_manager
        import utils.resource as _res

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        monkeypatch.setattr(ha_update_manager, "NOTIFY_WATCH_DIR", str(tmp_path))
        # Reset resource cache so tests don't leak disk state into each other.
        monkeypatch.setattr(_res, "_last_resource_status", None)
        ha_agent_advanced.init_local_database()
        return path

    def _good_disk(self):
        from utils.resource import ResourceStatus

        return ResourceStatus(
            disk_free_gb=5.0,
            disk_total_gb=32.0,
            disk_used_gb=27.0,
            mem_available_mb=512.0,
            mem_total_mb=1024.0,
            disk_warn=False,
            disk_critical=False,
            mem_warn=False,
        )

    def _low_disk(self):
        from utils.resource import ResourceStatus

        return ResourceStatus(
            disk_free_gb=1.8,
            disk_total_gb=32.0,
            disk_used_gb=30.2,
            mem_available_mb=512.0,
            mem_total_mb=1024.0,
            disk_warn=True,
            disk_critical=True,
            mem_warn=False,
        )

    def test_disk_ok_supervisor_current(self, db_path, monkeypatch):
        """Disk is fine and Supervisor is current — no problems, no enforcement."""
        import ha_update_manager
        import utils.resource as _res
        from ha_update_manager import run_update_preflight
        from utils.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(ha_update_manager, "HA_DISK_WARN_GB", 2.5)
        monkeypatch.setattr(_res, "_last_resource_status", self._good_disk())
        result = asyncio.run(
            run_update_preflight(rest_client=FakeHARestClient(states=[]))
        )

        assert result.disk_ok is True
        assert result.backups_purged_count == 0
        assert result.supervisor_status == "current"
        assert result.problems == []

    def test_disk_still_low_after_cleanup(self, db_path, monkeypatch):
        """Disk low, no eligible slugs — enforcement is no-op, problem reported."""
        import ha_agent_advanced
        import ha_update_manager
        import utils.resource as _res
        from ha_update_manager import run_update_preflight
        from utils.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(ha_update_manager, "HA_DISK_WARN_GB", 2.5)
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 10)
        monkeypatch.setattr(_res, "_last_resource_status", self._low_disk())

        async def fake_run(command, check=False):
            if "ha host info" in command:
                return 0, "disk_free: 1.8\ndisk_total: 32.0\ndisk_used: 30.2\n", ""
            if "/proc/meminfo" in command:
                return 0, _MEMINFO_SAMPLE, ""
            return 0, "", ""

        ssh = FakeSSHClient()
        ssh.run = fake_run  # type: ignore[method-assign]

        result = asyncio.run(
            run_update_preflight(
                ssh_client=ssh, rest_client=FakeHARestClient(states=[])
            )
        )

        assert result.backups_purged_count == 0
        assert result.disk_ok is False
        assert any("free disk manually" in p for p in result.problems)

    def test_supervisor_update_available_via_rest(self, db_path, monkeypatch):
        """REST shows a Supervisor update available — supervisor_status='update_available'."""
        import ha_update_manager
        import utils.resource as _res
        from ha_update_manager import run_update_preflight
        from utils.ha_rest_client import FakeHARestClient

        monkeypatch.setattr(ha_update_manager, "HA_DISK_WARN_GB", 2.5)
        monkeypatch.setattr(_res, "_last_resource_status", self._good_disk())

        rest = FakeHARestClient(
            states=[
                {
                    "entity_id": "update.home_assistant_supervisor_update",
                    "state": "on",
                    "attributes": {
                        "installed_version": "2024.06.0",
                        "latest_version": "2024.07.0",
                        "friendly_name": "Home Assistant Supervisor Update",
                    },
                }
            ]
        )
        result = asyncio.run(run_update_preflight(rest_client=rest))

        assert result.supervisor_status == "update_available"
        assert any("Supervisor" in p for p in result.problems)

    def test_preflight_embedded_in_core_update_card(
        self, db_path, monkeypatch, tmp_path
    ):
        """request_update_approval() for Core calls preflight and card body has component info."""
        import ha_update_manager
        import utils.resource as _res
        from ha_update_manager import request_update_approval
        from utils.ha_rest_client import FakeHARestClient, UpdateStatus
        from utils.notify import FakeNotifier

        monkeypatch.setattr(ha_update_manager, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(ha_update_manager, "HA_DISK_WARN_GB", 2.5)
        monkeypatch.setattr(_res, "_last_resource_status", self._good_disk())

        captured_body: list[str] = []

        class CapturingGate:
            async def require_approval(self, subject, body, payload, notifier, risk):
                captured_body.append(body)
                return False

        update = UpdateStatus(
            component="core",
            entity_id="update.home_assistant_core_update",
            installed_version="2026.7.0",
            latest_version="2026.8.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

        asyncio.run(
            request_update_approval(
                update=update,
                gate=CapturingGate(),  # type: ignore[arg-type]
                notifier=FakeNotifier(),
                rest_client=FakeHARestClient(states=[]),
                watch_dir=tmp_path,
            )
        )

        assert captured_body, "gate.require_approval was not called"
        body = captured_body[0]
        assert "Component: core" in body
        assert "Risk: CRITICAL" in body

    def test_preflight_purge_message_in_card_body(self, db_path, monkeypatch, tmp_path):
        """When backups are purged during preflight, card body includes the freed-space note."""
        import ha_agent_advanced
        import ha_update_manager
        import utils.resource as _res
        from ha_update_manager import request_update_approval
        from utils.ha_rest_client import FakeHARestClient, UpdateStatus
        from utils.notify import FakeNotifier

        monkeypatch.setattr(ha_update_manager, "NOTIFY_WATCH_DIR", str(tmp_path))
        monkeypatch.setattr(ha_update_manager, "HA_DISK_WARN_GB", 2.5)
        monkeypatch.setattr(ha_agent_advanced, "BACKUP_RETAIN_ON_HA", 1)
        monkeypatch.setattr(_res, "_last_resource_status", self._low_disk())

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry (backup_slug, timestamp, location) VALUES (?, ?, ?)",
                ("slug-recent", 2000, "both"),
            )
            conn.execute(
                "INSERT INTO backup_registry (backup_slug, timestamp, location) VALUES (?, ?, ?)",
                ("slug-old", 1000, "both"),
            )

        async def fake_run(command, check=False):
            if "ha host info" in command:
                return 0, "disk_free: 4.5\ndisk_total: 32.0\ndisk_used: 27.5\n", ""
            if "/proc/meminfo" in command:
                return 0, _MEMINFO_SAMPLE, ""
            if "ha backups remove" in command:
                return 0, "", ""
            return 0, "", ""

        ssh = FakeSSHClient()
        ssh.run = fake_run  # type: ignore[method-assign]

        captured_body: list[str] = []

        class CapturingGate:
            async def require_approval(self, subject, body, payload, notifier, risk):
                captured_body.append(body)
                return False

        update = UpdateStatus(
            component="core",
            entity_id="update.home_assistant_core_update",
            installed_version="2026.7.0",
            latest_version="2026.8.0",
            update_available=True,
            release_url=None,
            release_summary=None,
            in_progress=False,
        )

        asyncio.run(
            request_update_approval(
                update=update,
                gate=CapturingGate(),  # type: ignore[arg-type]
                notifier=FakeNotifier(),
                ssh_client=ssh,
                rest_client=FakeHARestClient(states=[]),
                watch_dir=tmp_path,
            )
        )

        assert captured_body, "gate.require_approval was not called"
        assert "Pre-flight:" in captured_body[0]
        assert "old backup" in captured_body[0]
