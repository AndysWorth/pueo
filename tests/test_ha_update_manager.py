"""Tests for ha_update_manager stuck-loop backoff behavior."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

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


def _make_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "update_test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(_HITL_DDL)
    return db_path


# ---------------------------------------------------------------------------
# Stuck-loop backoff — _run_update_analysis
# ---------------------------------------------------------------------------


class TestUpdateAnalysisStuckBackoff:
    def test_stuck_outcome_writes_backoff(self, tmp_path, pueo_dirs):
        """outcome='stuck' writes a deferred backoff row for the entity_id."""
        import asyncio
        import time
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_db(tmp_path)
        fake_result = AgentLoopResult(outcome="stuck")

        class _FakeUpdate:
            entity_id = "update.home_assistant_core_update"
            component = "Home Assistant Core"
            installed_version = "2026.9.0"
            latest_version = "2026.9.1"
            release_url = None
            release_summary = None

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
            mock.patch("agents.ha_update_manager.DB_PATH", db_path),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_update_manager import _run_update_analysis
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_update_analysis(
                    update=_FakeUpdate(),
                    notifier=FakeNotifier(),
                )
            )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_action, next_allowed_at FROM hitl_suppression"
                " WHERE card_key = "
                "'update_analysis_backoff:update.home_assistant_core_update'",
            ).fetchone()
        assert row is not None, "backoff row must exist after stuck outcome"
        assert row[0] == "deferred"
        assert row[1] > time.time()

    def test_success_outcome_no_backoff(self, tmp_path, pueo_dirs):
        """outcome='success' must not write a backoff row."""
        import asyncio
        import sqlite3
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_db(tmp_path)
        fake_result = AgentLoopResult(outcome="success")

        class _FakeUpdate:
            entity_id = "update.home_assistant_core_update"
            component = "Home Assistant Core"
            installed_version = "2026.9.0"
            latest_version = "2026.9.1"
            release_url = None
            release_summary = None

        with (
            mock.patch("utils.agent.agent_loop.AgentLoop") as MockLoop,
            mock.patch("utils.agent.supervisor.increment_active_agent"),
            mock.patch("utils.agent.supervisor.decrement_active_agent"),
            mock.patch(
                "utils.agent.supervisor.make_activity_timeline_callback",
                return_value=None,
            ),
            mock.patch(
                "utils.llm.llm_factory.make_llm_client", return_value=MagicMock()
            ),
            mock.patch("agents.ha_update_manager.DB_PATH", db_path),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=fake_result)
            MockLoop.return_value = mock_instance

            from agents.ha_update_manager import _run_update_analysis
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_update_analysis(
                    update=_FakeUpdate(),
                    notifier=FakeNotifier(),
                )
            )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_action FROM hitl_suppression"
                " WHERE card_key = "
                "'update_analysis_backoff:update.home_assistant_core_update'",
            ).fetchone()
        assert row is None, "success outcome must not write a backoff row"
