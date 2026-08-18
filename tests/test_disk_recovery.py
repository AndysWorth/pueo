"""Tests for utils/disk_recovery.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from utils.disk_recovery import (
    DISK_AUTO_ACTION_KEYS,
    RecoveryAction,
    RecoverySummary,
    TmpAuditItem,
    _parse_du_size,
    audit_supervisor_tmp,
    build_disk_conclusion,
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
    """Tests for truncate_ha_log with archiving disabled (archive tests are in TestArchiveOnTruncate)."""

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
        with patch("config.ARCHIVE_HA_LOG_ENABLED", False):
            result = asyncio.run(truncate_ha_log(ssh))
        assert result.success is True
        assert result.bytes_freed == 50 * 1024 * 1024
        assert result.name == "truncate_ha_log"

    def test_truncate_command_sent(self):
        ssh = self._make_ssh(1024)
        with patch("config.ARCHIVE_HA_LOG_ENABLED", False):
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
        with patch("config.ARCHIVE_HA_LOG_ENABLED", False):
            result = asyncio.run(truncate_ha_log(ssh))
        assert result.success is True
        assert result.bytes_freed == 0

    def test_ssh_exception_returns_failure(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(side_effect=Exception("connection lost"))
        with patch("config.ARCHIVE_HA_LOG_ENABLED", False):
            result = asyncio.run(truncate_ha_log(ssh))
        assert result.success is False
        assert result.bytes_freed == 0


class TestArchiveOnTruncate:
    """Verify archiver integration with truncate_ha_log."""

    def test_archives_before_truncating(self, tmp_path):
        from utils.archiver import archive_ha_log as _real_archive

        call_order = []

        async def mock_archive(ssh_client, archive_dir):
            call_order.append("archive")
            return None

        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                (0, "1048576", ""),  # stat (for truncate_ha_log)
                (0, "", ""),  # truncate
            ]
        )

        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", True),
            patch("config.PUEO_ARCHIVE_DIR", str(tmp_path)),
            patch("config.PUEO_ARCHIVE_MAX_GB", 2.0),
            patch("utils.disk_recovery.archive_ha_log", mock_archive),
            patch("utils.disk_recovery.enforce_archive_retention", lambda *a, **k: 0),
        ):
            result = asyncio.run(truncate_ha_log(ssh))

        assert "archive" in call_order
        # Truncate still succeeds even when archive returns None
        assert result.success is True

    def test_skips_archive_when_disabled(self, tmp_path):
        archived = []

        async def mock_archive(ssh_client, archive_dir):
            archived.append(True)
            return None

        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                (0, "1048576", ""),  # stat
                (0, "", ""),  # truncate
            ]
        )

        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", False),
            patch("utils.disk_recovery.archive_ha_log", mock_archive),
        ):
            asyncio.run(truncate_ha_log(ssh))

        assert len(archived) == 0


# ---------------------------------------------------------------------------
# vacuum_journal
# ---------------------------------------------------------------------------


class TestVacuumJournal:
    def _make_ssh(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                (0, "/usr/bin/journalctl", ""),  # which journalctl
                (0, "Archived and active journals take up 1.5G.", ""),  # disk-usage
                (0, "", ""),  # rotate
                (0, "Freed 800M of archived journals.", ""),  # vacuum
            ]
        )
        return ssh

    def test_success_returns_action(self):
        ssh = self._make_ssh()
        with patch("config.ARCHIVE_JOURNAL_ENABLED", False):
            result = asyncio.run(vacuum_journal(ssh, max_mb=200))
        assert result.success is True
        assert result.name == "vacuum_journal"

    def test_rotate_called_before_vacuum(self):
        ssh = self._make_ssh()
        with patch("config.ARCHIVE_JOURNAL_ENABLED", False):
            asyncio.run(vacuum_journal(ssh, max_mb=200))
        calls = [c.args[0] for c in ssh.run.call_args_list]
        rotate_idx = next(i for i, c in enumerate(calls) if "--rotate" in c)
        vacuum_idx = next(i for i, c in enumerate(calls) if "--vacuum-size" in c)
        assert rotate_idx < vacuum_idx

    def test_vacuum_size_param_respected(self):
        ssh = self._make_ssh()
        with patch("config.ARCHIVE_JOURNAL_ENABLED", False):
            asyncio.run(vacuum_journal(ssh, max_mb=512))
        calls = [c.args[0] for c in ssh.run.call_args_list]
        assert any("--vacuum-size=512M" in c for c in calls)

    def test_journalctl_not_available_returns_failure(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(side_effect=[(0, "", "")])  # which journalctl returns empty
        with patch("config.ARCHIVE_JOURNAL_ENABLED", False):
            result = asyncio.run(vacuum_journal(ssh))
        assert result.success is False
        assert "not available" in result.message

    def test_ssh_exception_returns_failure(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(side_effect=Exception("timeout"))
        with patch("config.ARCHIVE_JOURNAL_ENABLED", False):
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
            {"keep_days": 30, "repack": False},
        )

    def test_apply_filter_not_sent(self):
        rest = self._make_rest()
        asyncio.run(purge_recorder(rest, keep_days=30, repack=False))
        _, _, payload = rest.call_service.call_args.args
        assert "apply_filter" not in payload

    def test_repack_true_passed_through(self):
        rest = self._make_rest()
        asyncio.run(purge_recorder(rest, keep_days=7, repack=True))
        _, _, kwargs_payload = rest.call_service.call_args.args
        assert kwargs_payload["repack"] is True
        assert kwargs_payload["keep_days"] == 7

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
                # vacuum_journal: which, disk-usage, rotate, vacuum
                (0, "/usr/bin/journalctl", ""),
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
        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", False),
            patch("config.ARCHIVE_JOURNAL_ENABLED", False),
        ):
            summary = asyncio.run(
                run_safe_disk_recovery(ssh, rest, recorder_keep_days=30)
            )
        assert len(summary.actions) == 3
        names = [a.name for a in summary.actions]
        assert "truncate_ha_log" in names
        assert "vacuum_journal" in names
        assert "purge_recorder" in names

    def test_recorder_skipped_when_no_rest_client(self):
        ssh = self._make_ssh()
        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", False),
            patch("config.ARCHIVE_JOURNAL_ENABLED", False),
        ):
            summary = asyncio.run(run_safe_disk_recovery(ssh, rest_client=None))
        names = [a.name for a in summary.actions]
        assert "purge_recorder" not in names
        assert len(summary.actions) == 2

    def test_partial_failure_does_not_abort(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                Exception("stat failed"),  # truncate_ha_log stat raises
                (0, "/usr/bin/journalctl", ""),  # vacuum_journal which
                (0, "1G", ""),  # vacuum_journal disk-usage
                (0, "", ""),  # vacuum_journal rotate
                (0, "Freed 400M", ""),  # vacuum_journal vacuum-size
            ]
        )
        rest = MagicMock()
        rest.call_service = AsyncMock(return_value=None)
        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", False),
            patch("config.ARCHIVE_JOURNAL_ENABLED", False),
        ):
            summary = asyncio.run(run_safe_disk_recovery(ssh, rest))
        assert summary.actions[0].success is False
        assert summary.actions[1].success is True


# ---------------------------------------------------------------------------
# build_disk_conclusion
# ---------------------------------------------------------------------------

_HITL_OPTIONS = [
    {"action_key": "repack_recorder", "name": "Repack"},
    {"action_key": "aggressive_purge_7d", "name": "Purge"},
    {"action_key": "clean_tmp", "name": "Clean tmp"},
    {"action_key": "offload_backups", "name": "Offload"},
]


class TestBuildDiskConclusion:
    def _call(self, **kwargs):
        defaults = dict(
            top_consumers=[],
            addon_persistent=[],
            auto_savings_mb=0.0,
            disk_free_after_gb=1.9,
            disk_critical_gb=3.0,
            hitl_options=_HITL_OPTIONS,
            backup_section_bytes=0,
            ha_config_section_bytes=0,
        )
        defaults.update(kwargs)
        return build_disk_conclusion(**defaults)

    def test_returns_required_keys(self):
        result = self._call()
        for key in (
            "text",
            "hitl_sufficient",
            "needed_mb",
            "hitl_estimate_mb",
            "top_consumers",
            "addon_persistent",
        ):
            assert key in result

    def test_insufficient_when_savings_less_than_needed(self):
        # needs 1126 MB, backup=0, config=0 → hitl_estimate=0 → insufficient
        result = self._call(
            disk_free_after_gb=1.9,
            disk_critical_gb=3.0,
            backup_section_bytes=0,
            ha_config_section_bytes=0,
        )
        assert result["hitl_sufficient"] is False
        assert result["needed_mb"] == pytest.approx(1126.0, abs=1)
        assert (
            "not enough" in result["text"]
            or "not fully" in result["text"]
            or "not enough" in result["text"]
            or "unlikely" in result["text"]
            or "likely not" in result["text"]
        )

    def test_sufficient_when_backup_section_large_enough(self):
        # needs 1126 MB, backup section = 2 GB → estimate = 2048 MB → sufficient
        result = self._call(
            disk_free_after_gb=1.9,
            disk_critical_gb=3.0,
            backup_section_bytes=2 * 1024**3,
        )
        assert result["hitl_sufficient"] is True
        assert result["hitl_estimate_mb"] == pytest.approx(2048.0, abs=1)

    def test_sufficient_when_already_above_critical(self):
        # disk_free_after_gb already above threshold
        result = self._call(
            disk_free_after_gb=4.0,
            disk_critical_gb=3.0,
        )
        assert result["hitl_sufficient"] is True
        assert result["needed_mb"] == 0.0
        assert "above" in result["text"] or "threshold" in result["text"]

    def test_culprit_named_from_addon_persistent(self):
        addon_persistent = [
            {
                "name": "NetAlertX",
                "slug": "netalertx",
                "size_bytes": 7 * 1024**3,
                "size_human": "7.0 GB",
            }
        ]
        result = self._call(addon_persistent=addon_persistent)
        assert "NetAlertX" in result["text"]

    def test_culprit_falls_back_to_top_consumers(self):
        top_consumers = [
            {
                "name": "home-assistant_v2.db",
                "section": "HA Config & Database",
                "size_bytes": 5 * 1024**3,
                "size_human": "5.0 GB",
            }
        ]
        result = self._call(top_consumers=top_consumers, addon_persistent=[])
        assert "home-assistant_v2.db" in result["text"]

    def test_addon_persistent_capped_at_four(self):
        addon_persistent = [
            {
                "name": f"addon_{i}",
                "slug": f"slug_{i}",
                "size_bytes": i * 1024**3,
                "size_human": f"{i} GB",
            }
            for i in range(6, 0, -1)
        ]
        result = self._call(addon_persistent=addon_persistent)
        assert len(result["addon_persistent"]) == 4

    def test_top_consumers_capped_at_four(self):
        top_consumers = [
            {
                "name": f"item_{i}",
                "section": "HA Config",
                "size_bytes": i * 1024**2,
                "size_human": f"{i} MB",
            }
            for i in range(10, 0, -1)
        ]
        result = self._call(top_consumers=top_consumers)
        assert len(result["top_consumers"]) == 4

    def test_no_data_returns_valid_dict(self):
        result = self._call(
            top_consumers=[], addon_persistent=[], backup_section_bytes=0
        )
        assert isinstance(result["text"], str)
        assert len(result["text"]) > 0


# ---------------------------------------------------------------------------
# fetch_addon_persistent_sizes
# ---------------------------------------------------------------------------


class TestFetchAddonPersistentSizes:
    def test_parses_du_output_and_resolves_names(self):
        from utils.disk_usage import fetch_addon_persistent_sizes

        ssh = MagicMock()
        ssh.run = AsyncMock(
            return_value=(
                0,
                "7.2G\t/mnt/data/supervisor/addons/a0d7b954_netalertx\n"
                "1.1G\t/mnt/data/supervisor/addons/core_mosquitto\n",
                "",
            )
        )
        addon_name_map = {
            "a0d7b954_netalertx": "NetAlertX",
            "core_mosquitto": "Mosquitto Broker",
        }
        result = asyncio.run(fetch_addon_persistent_sizes(ssh, addon_name_map))
        assert len(result) == 2
        assert result[0]["name"] == "NetAlertX"
        assert result[0]["size_human"] == "7.2 GB"
        assert result[1]["name"] == "Mosquitto Broker"

    def test_returns_empty_on_ssh_exception(self):
        from utils.disk_usage import fetch_addon_persistent_sizes

        ssh = MagicMock()
        ssh.run = AsyncMock(side_effect=Exception("permission denied"))
        result = asyncio.run(fetch_addon_persistent_sizes(ssh, {}))
        assert result == []

    def test_returns_empty_on_no_output(self):
        from utils.disk_usage import fetch_addon_persistent_sizes

        ssh = MagicMock()
        ssh.run = AsyncMock(return_value=(0, "", ""))
        result = asyncio.run(fetch_addon_persistent_sizes(ssh, {}))
        assert result == []

    def test_unknown_slug_uses_slug_as_name(self):
        from utils.disk_usage import fetch_addon_persistent_sizes

        ssh = MagicMock()
        ssh.run = AsyncMock(
            return_value=(
                0,
                "500M\t/mnt/data/supervisor/addons/some_unknown_slug\n",
                "",
            )
        )
        result = asyncio.run(fetch_addon_persistent_sizes(ssh, {}))
        assert result[0]["name"] == "some_unknown_slug"

    def test_sorted_descending_by_size(self):
        from utils.disk_usage import fetch_addon_persistent_sizes

        ssh = MagicMock()
        ssh.run = AsyncMock(
            return_value=(
                0,
                "100M\t/mnt/data/supervisor/addons/small\n"
                "5.0G\t/mnt/data/supervisor/addons/large\n",
                "",
            )
        )
        result = asyncio.run(fetch_addon_persistent_sizes(ssh, {}))
        assert result[0]["slug"] == "large"
        assert result[1]["slug"] == "small"


# ---------------------------------------------------------------------------
# run_safe_disk_recovery — LLM-guided path (Chunk F)
# ---------------------------------------------------------------------------


class TestRunSafeDiskRecoveryLLMGuided:
    """Verify the investigation-guided path and its fallback behaviour."""

    def _make_ssh(self):
        ssh = MagicMock()
        ssh.run = AsyncMock(
            side_effect=[
                # truncate_ha_log: stat, truncate
                (0, str(5 * 1024 * 1024), ""),
                (0, "", ""),
                # vacuum_journal: which, disk-usage, rotate, vacuum
                (0, "/usr/bin/journalctl", ""),
                (0, "800M", ""),
                (0, "", ""),
                (0, "Freed 200M", ""),
            ]
        )
        return ssh

    def _make_investigation_report(self, auto_keys):
        """Return a minimal InvestigationReport-like object with the given auto action_keys."""
        from utils.investigation_loop import InvestigationAction, InvestigationReport

        return InvestigationReport(
            topic="HA disk space critically low",
            summary="Journal and log are the largest consumers.",
            root_causes=["oversized journal"],
            auto_actions=[
                InvestigationAction(
                    name=k,
                    description=f"Run {k}",
                    estimated_impact="~200 MB",
                    reversible=True,
                    risk_level="LOW",
                    action_key=k,
                )
                for k in auto_keys
            ],
            hitl_actions=[],
            confidence=0.85,
        )

    def test_uses_investigation_auto_actions_when_available(self):
        """Investigation succeeds: only the recommended actions run (not all 3 heuristic steps)."""
        report = self._make_investigation_report(["truncate_ha_log", "vacuum_journal"])
        ssh = self._make_ssh()
        rest = MagicMock()
        rest.call_service = AsyncMock(return_value=None)

        llm = MagicMock()

        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", False),
            patch("config.ARCHIVE_JOURNAL_ENABLED", False),
            patch(
                "utils.investigation_loop.investigate_with_fallback",
                new=AsyncMock(return_value=(report, False)),
            ),
        ):
            summary = asyncio.run(run_safe_disk_recovery(ssh, rest, llm_client=llm))

        assert summary.used_investigation is True
        assert summary.investigation_report is report
        names = [a.name for a in summary.actions]
        # Only the two LLM-recommended steps ran; purge_recorder was NOT called
        assert "truncate_ha_log" in names
        assert "vacuum_journal" in names
        assert "purge_recorder" not in names
        rest.call_service.assert_not_called()

    def test_falls_back_to_heuristic_when_investigation_raises(self):
        """investigate_with_fallback raising causes the heuristic 3-step path to run."""
        ssh = self._make_ssh()
        rest = MagicMock()
        rest.call_service = AsyncMock(return_value=None)
        llm = MagicMock()

        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", False),
            patch("config.ARCHIVE_JOURNAL_ENABLED", False),
            patch(
                "utils.investigation_loop.investigate_with_fallback",
                new=AsyncMock(return_value=(None, True)),  # is_fallback=True
            ),
        ):
            summary = asyncio.run(run_safe_disk_recovery(ssh, rest, llm_client=llm))

        assert summary.used_investigation is False
        names = [a.name for a in summary.actions]
        assert "truncate_ha_log" in names
        assert "vacuum_journal" in names
        assert "purge_recorder" in names

    def test_falls_back_to_heuristic_when_no_recognized_keys(self):
        """LLM returns unrecognized action_keys → heuristic runs, report stored."""
        report = self._make_investigation_report(["unknown_key", "another_unknown"])
        ssh = self._make_ssh()
        rest = MagicMock()
        rest.call_service = AsyncMock(return_value=None)
        llm = MagicMock()

        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", False),
            patch("config.ARCHIVE_JOURNAL_ENABLED", False),
            patch(
                "utils.investigation_loop.investigate_with_fallback",
                new=AsyncMock(return_value=(report, False)),
            ),
        ):
            summary = asyncio.run(run_safe_disk_recovery(ssh, rest, llm_client=llm))

        assert summary.used_investigation is False
        # Report is kept so callers can use it for disk_analysis narrative
        assert summary.investigation_report is report
        names = [a.name for a in summary.actions]
        assert set(names) >= {"truncate_ha_log", "vacuum_journal"}

    def test_action_key_dispatch_recognizes_expected_keys(self):
        """DISK_AUTO_ACTION_KEYS contains the three dispatch-table entries."""
        assert "truncate_ha_log" in DISK_AUTO_ACTION_KEYS
        assert "vacuum_journal" in DISK_AUTO_ACTION_KEYS
        assert "purge_recorder" in DISK_AUTO_ACTION_KEYS

    def test_no_investigation_without_llm_client(self):
        """Without llm_client, investigation is never called and heuristic always runs."""
        ssh = self._make_ssh()
        rest = MagicMock()
        rest.call_service = AsyncMock(return_value=None)

        with (
            patch("config.ARCHIVE_HA_LOG_ENABLED", False),
            patch("config.ARCHIVE_JOURNAL_ENABLED", False),
        ):
            summary = asyncio.run(run_safe_disk_recovery(ssh, rest))

        assert summary.investigation_report is None
        assert summary.used_investigation is False
        assert len(summary.actions) == 3
