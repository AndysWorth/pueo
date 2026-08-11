"""Tests for utils/disk_recovery.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from utils.disk_recovery import (
    RecoveryAction,
    RecoverySummary,
    TmpAuditItem,
    _parse_du_size,
    audit_supervisor_tmp,
    purge_recorder,
    run_safe_disk_recovery,
    truncate_ha_log,
    vacuum_journal,
)


# ---------------------------------------------------------------------------
# RecoverySummary properties
# ---------------------------------------------------------------------------


class TestRecoverySummary:
    def test_total_freed_mb_sums_successful_actions(self):
        s = RecoverySummary(
            actions=[
                RecoveryAction("a", 10 * 1024 * 1024, "ok", True),
                RecoveryAction("b", 0, "failed", False),
                RecoveryAction("c", 5 * 1024 * 1024, "ok", True),
            ]
        )
        assert s.total_freed_mb == pytest.approx(15.0)

    def test_total_freed_mb_ignores_failed(self):
        s = RecoverySummary(
            actions=[RecoveryAction("a", 1024 * 1024 * 1024, "ok", False)]
        )
        assert s.total_freed_mb == 0.0

    def test_succeeded_and_failed_partition(self):
        ok = RecoveryAction("ok", 0, "", True)
        fail = RecoveryAction("fail", 0, "", False)
        s = RecoverySummary(actions=[ok, fail])
        assert s.succeeded == [ok]
        assert s.failed == [fail]


# ---------------------------------------------------------------------------
# _parse_du_size
# ---------------------------------------------------------------------------


class TestParseDuSize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1.0G", 1024**3),
            ("500M", 500 * 1024**2),
            ("4.0K", 4 * 1024),
            ("1024B", 1024),
            ("2.5G", int(2.5 * 1024**3)),
            ("0M", 0),
            ("garbage", 0),
            ("", 0),
        ],
    )
    def test_parse(self, raw, expected):
        assert _parse_du_size(raw) == expected


# ---------------------------------------------------------------------------
# truncate_ha_log
# ---------------------------------------------------------------------------


class TestTruncateHaLog:
    def _make_ssh(self, size_bytes: int = 50 * 1024 * 1024):
        ssh = MagicMock()
        # First call: stat; second call: truncate
        ssh.run = AsyncMock(
            side_effect=[
                (0, str(size_bytes), ""),  # stat
                (0, "", ""),  # truncate
            ]
        )
        return ssh

    def test_returns_bytes_freed(self):
        ssh = self._make_ssh(50 * 1024 * 1024)
        result = asyncio.run(truncate_ha_log(ssh))
        assert result.success is True
        assert result.bytes_freed == 50 * 1024 * 1024
        assert result.name == "truncate_ha_log"

    def test_truncate_command_sent(self):
        ssh = self._make_ssh(1024)
        asyncio.run(truncate_ha_log(ssh))
        calls = [c.args[0] for c in ssh.run.call_args_list]
        assert any("truncate -s 0" in c for c in calls)

    def test_stat_failure_returns_zero_bytes(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                (0, "", ""),  # stat returns empty
                (0, "", ""),  # truncate
            ]
        )
        result = asyncio.run(truncate_ha_log(ssh))
        assert result.success is True
        assert result.bytes_freed == 0

    def test_ssh_exception_returns_failure(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(side_effect=Exception("connection lost"))
        result = asyncio.run(truncate_ha_log(ssh))
        assert result.success is False
        assert result.bytes_freed == 0


# ---------------------------------------------------------------------------
# vacuum_journal
# ---------------------------------------------------------------------------


class TestVacuumJournal:
    def _make_ssh(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                (0, "Archived and active journals take up 1.5G.", ""),  # disk-usage
                (0, "", ""),  # rotate
                (0, "Freed 800M of archived journals.", ""),  # vacuum
            ]
        )
        return ssh

    def test_success_returns_action(self):
        ssh = self._make_ssh()
        result = asyncio.run(vacuum_journal(ssh, max_mb=200))
        assert result.success is True
        assert result.name == "vacuum_journal"

    def test_rotate_called_before_vacuum(self):
        ssh = self._make_ssh()
        asyncio.run(vacuum_journal(ssh, max_mb=200))
        calls = [c.args[0] for c in ssh.run.call_args_list]
        rotate_idx = next(i for i, c in enumerate(calls) if "--rotate" in c)
        vacuum_idx = next(i for i, c in enumerate(calls) if "--vacuum-size" in c)
        assert rotate_idx < vacuum_idx

    def test_vacuum_size_param_respected(self):
        ssh = self._make_ssh()
        asyncio.run(vacuum_journal(ssh, max_mb=512))
        calls = [c.args[0] for c in ssh.run.call_args_list]
        assert any("--vacuum-size=512M" in c for c in calls)

    def test_ssh_exception_returns_failure(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(side_effect=Exception("timeout"))
        result = asyncio.run(vacuum_journal(ssh))
        assert result.success is False


# ---------------------------------------------------------------------------
# purge_recorder
# ---------------------------------------------------------------------------


class TestPurgeRecorder:
    def _make_rest(self):
        rest = MagicMock()
        rest.call_service = AsyncMock(return_value=None)
        return rest

    def test_calls_correct_service(self):
        rest = self._make_rest()
        asyncio.run(purge_recorder(rest, keep_days=30, repack=False))
        rest.call_service.assert_called_once_with(
            "recorder",
            "purge",
            {"purge_keep_days": 30, "repack": False, "apply_filter": True},
        )

    def test_repack_true_passed_through(self):
        rest = self._make_rest()
        asyncio.run(purge_recorder(rest, keep_days=7, repack=True))
        _, _, kwargs_payload = rest.call_service.call_args.args
        assert kwargs_payload["repack"] is True
        assert kwargs_payload["purge_keep_days"] == 7

    def test_success_returns_action(self):
        rest = self._make_rest()
        result = asyncio.run(purge_recorder(rest, keep_days=30))
        assert result.success is True
        assert result.name == "purge_recorder"

    def test_rest_exception_returns_failure(self):
        rest = MagicMock()
        rest.call_service = AsyncMock(side_effect=Exception("HTTP 500"))
        result = asyncio.run(purge_recorder(rest))
        assert result.success is False
        assert result.bytes_freed == 0


# ---------------------------------------------------------------------------
# audit_supervisor_tmp
# ---------------------------------------------------------------------------


class TestAuditSupervisorTmp:
    def _make_ssh(self, output: str):
        ssh = MagicMock()
        ssh.run = AsyncMock(return_value=(0, output, ""))
        return ssh

    def test_parses_du_output(self):
        ssh = self._make_ssh(
            "1.2G\t/mnt/data/supervisor/tmp/failed-backup-abc\n"
            "500M\t/mnt/data/supervisor/tmp/partial-xyz\n"
        )
        items = asyncio.run(audit_supervisor_tmp(ssh))
        assert len(items) == 2
        assert items[0].path == "/mnt/data/supervisor/tmp/failed-backup-abc"
        assert items[0].size_display == "1.2G"
        assert items[0].size_bytes_approx == int(1.2 * 1024**3)
        assert items[1].path == "/mnt/data/supervisor/tmp/partial-xyz"

    def test_empty_directory_returns_empty_list(self):
        ssh = self._make_ssh("")
        items = asyncio.run(audit_supervisor_tmp(ssh))
        assert items == []

    def test_ssh_exception_returns_empty_list(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(side_effect=Exception("permission denied"))
        items = asyncio.run(audit_supervisor_tmp(ssh))
        assert items == []

    def test_malformed_lines_skipped(self):
        ssh = self._make_ssh("not-a-valid-line\n500M\t/mnt/data/supervisor/tmp/good\n")
        items = asyncio.run(audit_supervisor_tmp(ssh))
        assert len(items) == 1
        assert items[0].path == "/mnt/data/supervisor/tmp/good"


# ---------------------------------------------------------------------------
# run_safe_disk_recovery
# ---------------------------------------------------------------------------


class TestRunSafeDiskRecovery:
    def _make_ssh(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                # truncate_ha_log: stat, truncate
                (0, str(10 * 1024 * 1024), ""),
                (0, "", ""),
                # vacuum_journal: disk-usage, rotate, vacuum
                (0, "1G", ""),
                (0, "", ""),
                (0, "Freed 400M", ""),
            ]
        )
        return ssh

    def test_all_three_steps_run(self):
        ssh = self._make_ssh()
        rest = MagicMock()
        rest.call_service = AsyncMock(return_value=None)
        summary = asyncio.run(run_safe_disk_recovery(ssh, rest, recorder_keep_days=30))
        assert len(summary.actions) == 3
        names = [a.name for a in summary.actions]
        assert "truncate_ha_log" in names
        assert "vacuum_journal" in names
        assert "purge_recorder" in names

    def test_recorder_skipped_when_no_rest_client(self):
        ssh = self._make_ssh()
        summary = asyncio.run(run_safe_disk_recovery(ssh, rest_client=None))
        names = [a.name for a in summary.actions]
        assert "purge_recorder" not in names
        assert len(summary.actions) == 2

    def test_partial_failure_does_not_abort(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                Exception("stat failed"),  # truncate_ha_log stat raises
                (0, "1G", ""),  # vacuum_journal disk-usage
                (0, "", ""),  # vacuum_journal rotate
                (0, "Freed 400M", ""),  # vacuum_journal vacuum-size
            ]
        )
        rest = MagicMock()
        rest.call_service = AsyncMock(return_value=None)
        summary = asyncio.run(run_safe_disk_recovery(ssh, rest))
        assert summary.actions[0].success is False
        assert summary.actions[1].success is True
