"""Unit tests for ha_log_monitor — _LogStreamState, source routing, sparkline helpers."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# _LogStreamState
# ---------------------------------------------------------------------------


class TestLogStreamState:
    def test_default_fields(self, pueo_dirs):
        from agents.ha_log_monitor import _LogStreamState

        state = _LogStreamState(loop_name="test_loop")
        assert state.loop_name == "test_loop"
        assert state.bucket_total == 0
        assert state.bucket_matches == 0
        assert state.last_line_at == 0.0
        assert state.last_match_at == 0.0

    def test_core_and_supervisor_states_are_distinct(self, pueo_dirs):
        from agents.ha_log_monitor import _core_state, _supervisor_state

        assert _core_state is not _supervisor_state
        assert _core_state.loop_name != _supervisor_state.loop_name

    def test_supervisor_state_loop_name(self, pueo_dirs):
        from agents.ha_log_monitor import _supervisor_state

        assert _supervisor_state.loop_name == "ha_log_monitor_supervisor"

    def test_core_state_loop_name(self, pueo_dirs):
        from agents.ha_log_monitor import _LOOP_NAME, _core_state

        assert _core_state.loop_name == _LOOP_NAME


# ---------------------------------------------------------------------------
# _get_stream_state
# ---------------------------------------------------------------------------


class TestGetStreamState:
    def test_supervisor_source_returns_supervisor_state(self, pueo_dirs):
        from agents.ha_log_monitor import _get_stream_state, _supervisor_state

        assert _get_stream_state("supervisor") is _supervisor_state

    def test_core_source_returns_core_state(self, pueo_dirs):
        from agents.ha_log_monitor import _core_state, _get_stream_state

        assert _get_stream_state("core") is _core_state

    def test_unknown_source_returns_core_state(self, pueo_dirs):
        from agents.ha_log_monitor import _core_state, _get_stream_state

        assert _get_stream_state("other") is _core_state


# ---------------------------------------------------------------------------
# _source_to_command
# ---------------------------------------------------------------------------


class TestSourceToCommand:
    def test_supervisor_command(self, pueo_dirs):
        from agents.ha_log_monitor import _source_to_command

        cmd = _source_to_command("supervisor")
        assert "supervisor" in cmd
        assert "--follow" in cmd

    def test_core_command(self, pueo_dirs):
        from agents.ha_log_monitor import _source_to_command

        cmd = _source_to_command("core")
        assert "core" in cmd
        assert "--follow" in cmd

    def test_default_is_core(self, pueo_dirs):
        from agents.ha_log_monitor import _source_to_command

        assert _source_to_command("other") == _source_to_command("core")


# ---------------------------------------------------------------------------
# _source_to_loop_name
# ---------------------------------------------------------------------------


class TestSourceToLoopName:
    def test_supervisor_loop_name(self, pueo_dirs):
        from agents.ha_log_monitor import _source_to_loop_name

        assert _source_to_loop_name("supervisor") == "ha_log_monitor_supervisor"

    def test_core_loop_name(self, pueo_dirs):
        from agents.ha_log_monitor import _LOOP_NAME, _source_to_loop_name

        assert _source_to_loop_name("core") == _LOOP_NAME


# ---------------------------------------------------------------------------
# get_sparkline_data_for_loop / backward-compat alias
# ---------------------------------------------------------------------------


class TestGetSparklineDataForLoop:
    def test_returns_dict_with_required_keys(self, pueo_dirs, tmp_path):
        from unittest.mock import patch

        import agents.ha_log_monitor as mon
        import config as _cfg

        db_path = tmp_path / "spark.db"
        with patch.object(_cfg, "DB_PATH", str(db_path)):
            result = mon.get_sparkline_data_for_loop(
                mon._LOOP_NAME, bucket_size="1m", time_range="1h"
            )
        assert isinstance(result, dict)
        for key in ("buckets", "last_line_at", "last_match_at"):
            assert key in result

    def test_backward_compat_alias(self, pueo_dirs):
        from unittest.mock import patch

        import agents.ha_log_monitor as mon

        with patch.object(mon, "get_sparkline_data_for_loop") as mock_fn:
            mock_fn.return_value = {
                "buckets": [],
                "last_line_at": 0,
                "last_match_at": 0,
            }
            result = mon.get_ha_log_sparkline_data(bucket_size="1m", time_range="1h")
            mock_fn.assert_called_once_with(
                mon._LOOP_NAME, bucket_size="1m", time_range="1h"
            )
        assert result is not None


# ---------------------------------------------------------------------------
# Stuck-loop backoff — _run_repair_issue_investigation
# ---------------------------------------------------------------------------

_HITL_DDL = """
CREATE TABLE IF NOT EXISTS hitl_suppression (
    card_key TEXT PRIMARY KEY,
    card_type TEXT DEFAULT '',
    description TEXT DEFAULT '',
    first_sent_at REAL DEFAULT 0,
    last_sent_at REAL DEFAULT 0,
    send_count INTEGER DEFAULT 1,
    known_issue INTEGER DEFAULT 0,
    known_issue_note TEXT DEFAULT '',
    last_action TEXT,
    last_action_at REAL,
    rejection_count INTEGER DEFAULT 0,
    next_allowed_at REAL,
    resolved_at REAL
)
"""

_HA_REPAIR_DDL = """
CREATE TABLE IF NOT EXISTS ha_repair_history (
    issue_key TEXT PRIMARY KEY,
    translation_key TEXT,
    domain TEXT,
    first_seen_at REAL,
    last_seen_at REAL,
    hitl_sent_at REAL,
    resolved_at REAL
)
"""


def _make_repair_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "repair_test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(_HITL_DDL)
        conn.execute(_HA_REPAIR_DDL)
    return db_path


class TestRepairIssueStuckBackoff:
    """When _run_repair_issue_investigation returns non-success, a backoff row must be written."""

    def test_stuck_outcome_writes_backoff(self, tmp_path):
        """outcome='stuck' writes a deferred row for the issue key."""
        import asyncio
        import sqlite3
        import time
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_repair_db(tmp_path)
        fake_result = AgentLoopResult(outcome="stuck")

        class _FakeIssue:
            issue_key = "test-issue-uuid"
            issue_id = "test-issue-uuid"
            domain = "homeassistant"
            severity = "warning"
            translation_key = "reboot_required"
            breaks_in_ha_version = None
            data: dict = {}

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch("utils.agent.supervisor.publish_activity_done"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_log_monitor import _run_repair_issue_investigation
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_repair_issue_investigation(
                    issue=_FakeIssue(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_action, next_allowed_at FROM hitl_suppression"
                " WHERE card_key = 'repair_issue_backoff:test-issue-uuid'",
            ).fetchone()
        assert row is not None, "backoff row must exist after stuck outcome"
        assert row[0] == "deferred"
        assert row[1] > time.time()

    def test_success_outcome_no_backoff(self, tmp_path):
        """outcome='success' must not write a backoff row."""
        import asyncio
        import sqlite3
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_repair_db(tmp_path)
        fake_result = AgentLoopResult(outcome="success")

        class _FakeIssue:
            issue_key = "success-uuid"
            issue_id = "success-uuid"
            domain = "homeassistant"
            severity = "warning"
            translation_key = None
            breaks_in_ha_version = None
            data: dict = {}

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch("utils.agent.supervisor.publish_activity_done"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_log_monitor import _run_repair_issue_investigation
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_repair_issue_investigation(
                    issue=_FakeIssue(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_action FROM hitl_suppression"
                " WHERE card_key = 'repair_issue_backoff:success-uuid'",
            ).fetchone()
        assert row is None, "success outcome must not write a backoff row"


# ---------------------------------------------------------------------------
# ha_profile injection into repair executor (Fix 2)
# ---------------------------------------------------------------------------


class TestRepairIssueExecutorHaProfile:
    """_run_repair_issue_investigation injects ha_profile from DB into executor."""

    def test_ha_profile_injected_from_db(self, tmp_path, pueo_dirs):
        """When load_environment_profile returns a profile, executor._ha_profile is set."""
        import asyncio
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_repair_db(tmp_path)
        fake_result = AgentLoopResult(outcome="success", episode_stub={"summary": "ok"})

        class _FakeIssue:
            issue_key = "profile-test-uuid"
            issue_id = "profile-test-uuid"
            domain = "homeassistant"
            severity = "warning"
            translation_key = "reboot_required"
            breaks_in_ha_version = None
            data: dict = {}

        # Build a minimal fake HAEnvironmentProfile
        from utils.ha.ha_environment import HAEnvironmentProfile

        fake_profile = HAEnvironmentProfile()
        fake_profile.ha_version = "2026.9.2"

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch("utils.agent.supervisor.publish_activity_done"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
            mock.patch(
                "utils.ha.ha_environment.load_environment_profile",
                return_value=fake_profile,
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_log_monitor import _run_repair_issue_investigation
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_repair_issue_investigation(
                    issue=_FakeIssue(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )

        # AgentLoop was called with tool_executor= as a kwarg
        assert MockLoop.called, "AgentLoop must have been constructed"
        executor = MockLoop.call_args.kwargs.get("tool_executor")
        assert executor is not None, "tool_executor must be passed to AgentLoop"
        assert (
            executor._ha_profile is not None
        ), "executor._ha_profile must be set after _run_repair_issue_investigation"
        assert executor._ha_profile.ha_version == "2026.9.2"
