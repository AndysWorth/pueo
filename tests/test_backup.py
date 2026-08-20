"""Tests for the backup lifecycle fix (fix/backup-lifecycle PR).

Covers: trigger_backup tool chain, allowlist enforcement, disk-critical guard,
offload-failure path.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Allowlist assertions
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_ha_backups_new_not_in_allowlist(self):
        from utils.tool_executor import _HA_COMMAND_ALLOWLIST as allowlist

        assert "ha backups new" not in allowlist

    def test_ha_backups_list_still_in_allowlist(self):
        from utils.tool_executor import _HA_COMMAND_ALLOWLIST as allowlist

        assert "ha backups list" in allowlist

    def test_trigger_backup_in_chat_registry(self):
        from utils.tool_registry import build_chat_tool_registry

        reg = build_chat_tool_registry()
        assert "trigger_backup" in reg

    def test_trigger_backup_in_ha_registry(self):
        from utils.tool_registry import build_ha_tool_registry

        reg = build_ha_tool_registry()
        assert "trigger_backup" in reg


# ---------------------------------------------------------------------------
# trigger_backup full chain
# ---------------------------------------------------------------------------


class TestTriggerBackupChain:
    @pytest.fixture
    def db_path(self, tmp_path):
        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE backup_registry "
                "(id INTEGER PRIMARY KEY, timestamp INTEGER, backup_slug TEXT, "
                "status TEXT, size_bytes INTEGER, location TEXT, offloaded_at REAL, "
                "deleted_from_ha_at REAL)"
            )
        return db

    @pytest.fixture
    def executor(self, db_path):
        from utils.autonomy import FakeAutonomyGate
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        from utils.tool_executor import ToolExecutor

        ssh = FakeSSHClient(
            file_contents={},
            command_results={},
        )
        return ToolExecutor(
            ha_ssh_client=ssh,
            gate=FakeAutonomyGate(auto_execute_result=True, approval_result=True),
            notifier=FakeNotifier(),
            db_path=db_path,
        )

    def test_trigger_backup_calls_full_chain(self, executor, db_path):
        slug = "abc12345"
        with (
            patch(
                "agents.ha_agent_advanced.execute_remote_backup",
                new=AsyncMock(return_value=slug),
            ),
            patch("agents.ha_agent_advanced.record_backup_slug") as mock_record,
            patch(
                "agents.ha_agent_advanced.offload_backup_to_local",
                new=AsyncMock(return_value=True),
            ),
            patch("agents.ha_agent_advanced.enforce_ha_retention", new=AsyncMock()),
            patch("agents.ha_agent_advanced.purge_local_backups"),
        ):
            result = asyncio.run(executor._trigger_backup())

        assert result.success is True
        assert slug in result.output
        mock_record.assert_called_once_with(slug)

    def test_trigger_backup_disk_critical_blocked(self, executor):
        from utils.resource import DiskCriticalError

        with patch(
            "agents.ha_agent_advanced.execute_remote_backup",
            new=AsyncMock(side_effect=DiskCriticalError("disk full")),
        ):
            result = asyncio.run(executor._trigger_backup())

        assert result.success is False
        assert result.error is not None
        assert "disk" in result.error.lower()

    def test_trigger_backup_offload_failure_still_records_slug(self, executor, db_path):
        slug = "deadbeef"
        with (
            patch(
                "agents.ha_agent_advanced.execute_remote_backup",
                new=AsyncMock(return_value=slug),
            ),
            patch("agents.ha_agent_advanced.record_backup_slug") as mock_record,
            patch(
                "agents.ha_agent_advanced.offload_backup_to_local",
                new=AsyncMock(return_value=False),
            ),
            patch("agents.ha_agent_advanced.enforce_ha_retention", new=AsyncMock()),
            patch("agents.ha_agent_advanced.purge_local_backups"),
        ):
            result = asyncio.run(executor._trigger_backup())

        assert result.success is True
        mock_record.assert_called_once_with(slug)
        assert "offloaded=False" in result.output

    def test_run_ha_command_backups_new_rejected(self, executor):
        from utils.tool_registry import ToolCall

        call = ToolCall(name="run_ha_command", arguments={"command": "ha backups new"})
        result = asyncio.run(executor.execute(call))
        assert result.success is False
        assert "allowlist" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Backup naming
# ---------------------------------------------------------------------------


class TestBackupNaming:
    def test_execute_remote_backup_uses_timestamped_name(self):
        from agents import ha_agent_advanced

        src = inspect.getsource(ha_agent_advanced.execute_remote_backup)
        assert "Agent_PreFix_Snapshot" not in src
        assert "Pueo_" in src


# ---------------------------------------------------------------------------
# ha_agent_sandbox_engine re-exports
# ---------------------------------------------------------------------------


class TestSandboxEngineReexports:
    def test_execute_remote_backup_importable_from_sandbox_engine(self):
        from agents.ha_agent_sandbox_engine import execute_remote_backup  # noqa: F401

        assert callable(execute_remote_backup)

    def test_record_backup_slug_importable_from_sandbox_engine(self):
        from agents.ha_agent_sandbox_engine import record_backup_slug  # noqa: F401

        assert callable(record_backup_slug)

    def test_no_duplicate_implementation_in_sandbox_engine(self):
        """The sandbox engine re-exports from ha_agent_advanced, not its own copy."""
        from agents import ha_agent_advanced
        from agents import ha_agent_sandbox_engine

        assert (
            ha_agent_sandbox_engine.execute_remote_backup
            is ha_agent_advanced.execute_remote_backup
        )


# ---------------------------------------------------------------------------
# offload_pending_backups
# ---------------------------------------------------------------------------


class TestOffloadPendingBackups:
    @pytest.fixture
    def db_path(self, tmp_path):
        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE backup_registry "
                "(id INTEGER PRIMARY KEY, timestamp INTEGER, backup_slug TEXT, "
                "status TEXT, size_bytes INTEGER, location TEXT, offloaded_at REAL, "
                "deleted_from_ha_at REAL)"
            )
            conn.executemany(
                "INSERT INTO backup_registry"
                " (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, ?, 'ACTIVE', 1000000, 'ha')",
                [(1000, "slug-old"), (2000, "slug-new")],
            )
        return db

    def test_offload_pending_calls_offload_for_each_ha_slug(self, db_path):
        with (
            patch("agents.ha_agent_advanced.DB_PATH", db_path),
            patch(
                "agents.ha_agent_advanced.offload_backup_to_local",
                new=AsyncMock(return_value=True),
            ) as mock_offload,
            patch("agents.ha_agent_advanced.enforce_ha_retention", new=AsyncMock()),
            patch("agents.ha_agent_advanced.purge_local_backups"),
        ):
            asyncio.run(
                __import__(
                    "agents.ha_agent_advanced", fromlist=[""]
                ).offload_pending_backups()
            )

        assert mock_offload.call_count == 2
        called_slugs = {c.args[0] for c in mock_offload.call_args_list}
        assert called_slugs == {"slug-old", "slug-new"}

    def test_offload_pending_skips_already_offloaded(self, db_path):
        import time

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE backup_registry SET offloaded_at = ?, location = 'both'"
                " WHERE backup_slug = 'slug-old'",
                (time.time(),),
            )

        with (
            patch("agents.ha_agent_advanced.DB_PATH", db_path),
            patch(
                "agents.ha_agent_advanced.offload_backup_to_local",
                new=AsyncMock(return_value=True),
            ) as mock_offload,
            patch("agents.ha_agent_advanced.enforce_ha_retention", new=AsyncMock()),
            patch("agents.ha_agent_advanced.purge_local_backups"),
        ):
            asyncio.run(
                __import__(
                    "agents.ha_agent_advanced", fromlist=[""]
                ).offload_pending_backups()
            )

        assert mock_offload.call_count == 1
        assert mock_offload.call_args.args[0] == "slug-new"

    def test_offload_pending_noop_when_all_offloaded(self, db_path):
        import time

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE backup_registry SET offloaded_at = ?, location = 'both'",
                (time.time(),),
            )

        with (
            patch("agents.ha_agent_advanced.DB_PATH", db_path),
            patch(
                "agents.ha_agent_advanced.offload_backup_to_local",
                new=AsyncMock(return_value=True),
            ) as mock_offload,
            patch("agents.ha_agent_advanced.enforce_ha_retention", new=AsyncMock()),
            patch("agents.ha_agent_advanced.purge_local_backups"),
        ):
            asyncio.run(
                __import__(
                    "agents.ha_agent_advanced", fromlist=[""]
                ).offload_pending_backups()
            )

        mock_offload.assert_not_called()

    def test_offload_pending_partial_failure_continues(self, db_path):
        with (
            patch("agents.ha_agent_advanced.DB_PATH", db_path),
            patch(
                "agents.ha_agent_advanced.offload_backup_to_local",
                new=AsyncMock(side_effect=[False, True]),
            ) as mock_offload,
            patch("agents.ha_agent_advanced.enforce_ha_retention", new=AsyncMock()),
            patch("agents.ha_agent_advanced.purge_local_backups"),
        ):
            asyncio.run(
                __import__(
                    "agents.ha_agent_advanced", fromlist=[""]
                ).offload_pending_backups()
            )

        assert mock_offload.call_count == 2


# ---------------------------------------------------------------------------
# purge_local_backups location tracking
# ---------------------------------------------------------------------------


class TestPurgeLocalBackupsLocation:
    @pytest.fixture
    def db_path(self, tmp_path):
        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE backup_registry "
                "(id INTEGER PRIMARY KEY, timestamp INTEGER, backup_slug TEXT, "
                "status TEXT, size_bytes INTEGER, location TEXT, offloaded_at REAL, "
                "deleted_from_ha_at REAL)"
            )
        return db

    def _insert_offloaded(self, conn, slug, offload_age_days, deleted_from_ha=False):
        import time

        ts = time.time() - (offload_age_days * 86400)
        deleted = ts if deleted_from_ha else None
        conn.execute(
            "INSERT INTO backup_registry"
            " (timestamp, backup_slug, status, size_bytes, location,"
            "  offloaded_at, deleted_from_ha_at)"
            " VALUES (?, ?, 'ACTIVE', 0, 'both', ?, ?)",
            (int(ts), slug, ts, deleted),
        )

    def test_purge_sets_location_ha_when_still_on_ha(self, tmp_path, db_path):
        from agents.ha_agent_advanced import purge_local_backups

        # Need two slugs: purge skips the most-recent one, so "old" must not be newest.
        with sqlite3.connect(db_path) as conn:
            self._insert_offloaded(conn, "newer-slug", offload_age_days=31)
            self._insert_offloaded(conn, "old-still-on-ha", offload_age_days=40)

        with (
            patch("agents.ha_agent_advanced.DB_PATH", db_path),
            patch("agents.ha_agent_advanced.BACKUP_LOCAL_DIR", str(tmp_path)),
            patch("agents.ha_agent_advanced.BACKUP_RETAIN_LOCAL_DAYS", 30),
        ):
            purge_local_backups()

        with sqlite3.connect(db_path) as conn:
            loc = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = ?",
                ("old-still-on-ha",),
            ).fetchone()[0]
        assert loc == "ha"

    def test_purge_sets_location_purged_when_also_deleted_from_ha(
        self, tmp_path, db_path
    ):
        from agents.ha_agent_advanced import purge_local_backups

        # Need two slugs for the same reason; target has deleted_from_ha set.
        with sqlite3.connect(db_path) as conn:
            self._insert_offloaded(conn, "newer-slug", offload_age_days=31)
            self._insert_offloaded(
                conn, "gone-everywhere", offload_age_days=40, deleted_from_ha=True
            )

        with (
            patch("agents.ha_agent_advanced.DB_PATH", db_path),
            patch("agents.ha_agent_advanced.BACKUP_LOCAL_DIR", str(tmp_path)),
            patch("agents.ha_agent_advanced.BACKUP_RETAIN_LOCAL_DAYS", 30),
        ):
            purge_local_backups()

        with sqlite3.connect(db_path) as conn:
            loc = conn.execute(
                "SELECT location FROM backup_registry WHERE backup_slug = ?",
                ("gone-everywhere",),
            ).fetchone()[0]
        assert loc == "purged"


# ---------------------------------------------------------------------------
# _resolve_backup_remote_path: slug validation
# ---------------------------------------------------------------------------


class TestResolveBackupRemotePathSlugValidation:
    def test_unknown_slug_returns_none_without_ssh(self):
        """'unknown_slug' (with underscore) fails _SLUG_RE and returns None immediately."""
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient()
        result = asyncio.run(
            __import__(
                "agents.ha_agent_advanced", fromlist=[""]
            )._resolve_backup_remote_path("unknown_slug", ssh)
        )
        assert result is None
        assert ssh.commands_run == []

    def test_valid_hex_slug_proceeds_to_ssh(self):
        """A valid all-hex slug passes _SLUG_RE and makes at least one SSH call."""
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(command_results={})
        asyncio.run(
            __import__(
                "agents.ha_agent_advanced", fromlist=[""]
            )._resolve_backup_remote_path("abc123ef", ssh)
        )
        assert len(ssh.commands_run) >= 1


# ---------------------------------------------------------------------------
# enforce_ha_retention force_critical flag
# ---------------------------------------------------------------------------


class TestEnforceHaRetentionForceCritical:
    """Tests for the force_critical parameter added to enforce_ha_retention."""

    @pytest.fixture
    def db_path(self, tmp_path):
        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE backup_registry "
                "(id INTEGER PRIMARY KEY, timestamp INTEGER, backup_slug TEXT, "
                "status TEXT, size_bytes INTEGER, location TEXT, offloaded_at REAL, "
                "deleted_from_ha_at REAL)"
            )
        return db

    def _insert_backup(self, db_path, slug, ts, location="both"):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_registry"
                " (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, ?, 'ACTIVE', 0, ?)",
                (ts, slug, location),
            )

    def test_force_critical_deletes_when_at_floor(self, db_path):
        """force_critical=True lowers floor to 1; 2 offloaded backups → 1 deleted."""
        self._insert_backup(db_path, "newer-slug", ts=2000, location="both")
        self._insert_backup(db_path, "older-slug", ts=1000, location="both")

        ssh_calls = []

        class _FakeSSH:
            async def run(self, cmd, check=False):
                ssh_calls.append(cmd)
                return (0, "", "")

        with (
            patch("agents.ha_agent_advanced.DB_PATH", db_path),
            patch("agents.ha_agent_advanced.BACKUP_RETAIN_ON_HA", 2),
        ):
            deleted = asyncio.run(
                __import__(
                    "agents.ha_agent_advanced", fromlist=[""]
                ).enforce_ha_retention(ssh_client=_FakeSSH(), force_critical=True)
            )

        assert deleted == 1
        remove_calls = [c for c in ssh_calls if "ha backups remove" in c]
        assert len(remove_calls) == 1

    def test_force_critical_protects_newest_slug(self, db_path):
        """force_critical=True always keeps the newest slug even when only 1 on HA."""
        self._insert_backup(db_path, "only-slug", ts=1000, location="both")

        ssh_calls = []

        class _FakeSSH:
            async def run(self, cmd, check=False):
                ssh_calls.append(cmd)
                return (0, "", "")

        with patch("agents.ha_agent_advanced.DB_PATH", db_path):
            deleted = asyncio.run(
                __import__(
                    "agents.ha_agent_advanced", fromlist=[""]
                ).enforce_ha_retention(ssh_client=_FakeSSH(), force_critical=True)
            )

        assert deleted == 0  # 1 backup, floor=1 → nothing to delete
        assert not any("ha backups remove" in c for c in ssh_calls)

    def test_no_force_critical_bails_at_normal_floor(self, db_path):
        """Without force_critical, enforce_ha_retention bails when count == BACKUP_RETAIN_ON_HA."""
        self._insert_backup(db_path, "slug-a", ts=1000, location="both")
        self._insert_backup(db_path, "slug-b", ts=2000, location="both")

        ssh_calls = []

        class _FakeSSH:
            async def run(self, cmd, check=False):
                ssh_calls.append(cmd)
                return (0, "", "")

        with (
            patch("agents.ha_agent_advanced.DB_PATH", db_path),
            patch("agents.ha_agent_advanced.BACKUP_RETAIN_ON_HA", 2),
        ):
            deleted = asyncio.run(
                __import__(
                    "agents.ha_agent_advanced", fromlist=[""]
                ).enforce_ha_retention(ssh_client=_FakeSSH(), force_critical=False)
            )

        assert deleted == 0
        assert not any("ha backups remove" in c for c in ssh_calls)


# ---------------------------------------------------------------------------
# Supervisor backup_sync loop registration
# ---------------------------------------------------------------------------


class TestBackupSyncLoopRegistration:
    def test_backup_sync_loop_always_starts(self):
        """backup_sync (periodic reconcile+offload) is always registered in supervisor."""
        import inspect
        import main as m

        src = inspect.getsource(m.supervisor_main)
        assert 'supervisor.start("backup_sync"' in src

    def test_backup_reconcile_loop_calls_offload_pending(self):
        """The backup_sync loop body calls offload_pending_backups after reconcile."""
        import inspect
        import main as m

        src = inspect.getsource(m.supervisor_main)
        assert "offload_pending_backups" in src
