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
                "ha_agent_advanced.execute_remote_backup",
                new=AsyncMock(return_value=slug),
            ),
            patch("ha_agent_advanced.record_backup_slug") as mock_record,
            patch(
                "ha_agent_advanced.offload_backup_to_local",
                new=AsyncMock(return_value=True),
            ),
            patch("ha_agent_advanced.enforce_ha_retention", new=AsyncMock()),
            patch("ha_agent_advanced.purge_local_backups"),
        ):
            result = asyncio.run(executor._trigger_backup())

        assert result.success is True
        assert slug in result.output
        mock_record.assert_called_once_with(slug)

    def test_trigger_backup_disk_critical_blocked(self, executor):
        from utils.resource import DiskCriticalError

        with patch(
            "ha_agent_advanced.execute_remote_backup",
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
                "ha_agent_advanced.execute_remote_backup",
                new=AsyncMock(return_value=slug),
            ),
            patch("ha_agent_advanced.record_backup_slug") as mock_record,
            patch(
                "ha_agent_advanced.offload_backup_to_local",
                new=AsyncMock(return_value=False),
            ),
            patch("ha_agent_advanced.enforce_ha_retention", new=AsyncMock()),
            patch("ha_agent_advanced.purge_local_backups"),
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
        import ha_agent_advanced

        src = inspect.getsource(ha_agent_advanced.execute_remote_backup)
        assert "Agent_PreFix_Snapshot" not in src
        assert "Pueo_" in src


# ---------------------------------------------------------------------------
# ha_agent_sandbox_engine re-exports
# ---------------------------------------------------------------------------


class TestSandboxEngineReexports:
    def test_execute_remote_backup_importable_from_sandbox_engine(self):
        from ha_agent_sandbox_engine import execute_remote_backup  # noqa: F401

        assert callable(execute_remote_backup)

    def test_record_backup_slug_importable_from_sandbox_engine(self):
        from ha_agent_sandbox_engine import record_backup_slug  # noqa: F401

        assert callable(record_backup_slug)

    def test_no_duplicate_implementation_in_sandbox_engine(self):
        """The sandbox engine re-exports from ha_agent_advanced, not its own copy."""
        import ha_agent_advanced
        import ha_agent_sandbox_engine

        assert (
            ha_agent_sandbox_engine.execute_remote_backup
            is ha_agent_advanced.execute_remote_backup
        )
