"""Unit tests for ha_log_monitor — _LogStreamState, source routing, sparkline helpers."""

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

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


class TestRepairIssuePostUpdateSSH:
    """Repair investigation keeps an inert ha_ssh but gets a real post-update SSH (#846)."""

    def test_ssh_client_wired_only_to_post_update_path(self, tmp_path):
        import asyncio
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult
        from utils.ha.ssh_client import FakeSSHClient

        db_path = _make_repair_db(tmp_path)

        class _FakeIssue:
            issue_key = "hacs/restart_required_1"
            issue_id = "restart_required_1"
            domain = "hacs"
            severity = "warning"
            translation_key = "restart_required"
            breaks_in_ha_version = None
            data: dict = {}

        ssh = FakeSSHClient()
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
            mock_instance.run = AsyncMock(
                return_value=AgentLoopResult(outcome="success")
            )
            MockLoop.return_value = mock_instance

            from agents.ha_log_monitor import _run_repair_issue_investigation
            from utils.hitl.notify import FakeNotifier

            asyncio.run(
                _run_repair_issue_investigation(
                    issue=_FakeIssue(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                    ssh_client=ssh,
                )
            )

        executor = MockLoop.call_args.kwargs["tool_executor"]
        assert executor._post_update_ssh is ssh
        assert executor._ha_ssh is not ssh


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


class TestRepairIssueGateInjection:
    """_run_repair_issue_investigation accepts an injected gate (Codecov branch coverage)."""

    def test_injected_gate_is_used(self, tmp_path, pueo_dirs):
        """Passing gate= explicitly covers the 'gate is not None' branch."""
        import asyncio
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.autonomy import FakeAutonomyGate
        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_repair_db(tmp_path)
        fake_result = AgentLoopResult(outcome="success")
        injected_gate = FakeAutonomyGate(auto_execute_result=True)

        class _FakeIssue:
            issue_key = "gate-inject-uuid"
            issue_id = "gate-inject-uuid"
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
                    gate=injected_gate,
                )
            )

        executor = MockLoop.call_args.kwargs["tool_executor"]
        assert executor._gate is injected_gate


# ---------------------------------------------------------------------------
# WS-client + event-subscriber injection (ADR 017) + placeholder pre-inject
# ---------------------------------------------------------------------------


class _FakeRepairIssueWS:
    """Minimal HARepairIssue-like object with rich data fields."""

    issue_key = "ws-test-uuid"
    issue_id = "ws-test-uuid"
    domain = "spook"
    severity = "warning"
    translation_key = "empty_areas"
    breaks_in_ha_version = None
    data = {
        "translation_placeholders": {"areas": "Attic, Basement"},
        "learn_more_url": "https://my.home-assistant.io/redirect/spook",
        "is_fixable": True,
        "severity": "warning",
    }


class TestRepairIssueWSInjection:
    """_run_repair_issue_investigation threads ha_ws_client and event_subscriber
    into ToolExecutor (ADR 017)."""

    def _run(self, tmp_path, ha_ws_client=None, event_subscriber=None):
        import asyncio
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_repair_db(tmp_path)
        fake_result = AgentLoopResult(outcome="success")

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
                    issue=_FakeRepairIssueWS(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                    ha_ws_client=ha_ws_client,
                    event_subscriber=event_subscriber,
                )
            )
        return MockLoop

    def test_ws_client_injected_into_executor(self, tmp_path, pueo_dirs):
        """ha_ws_client= is forwarded to ToolExecutor._ws_client."""
        ws = object()
        MockLoop = self._run(tmp_path, ha_ws_client=ws)
        executor = MockLoop.call_args.kwargs["tool_executor"]
        assert executor._ws_client is ws

    def test_event_subscriber_set_on_executor(self, tmp_path, pueo_dirs):
        """event_subscriber is passed to executor.set_event_subscriber()."""
        sub = object()
        MockLoop = self._run(tmp_path, event_subscriber=sub)
        executor = MockLoop.call_args.kwargs["tool_executor"]
        assert executor._event_subscriber is sub

    def test_no_ws_client_leaves_none(self, tmp_path, pueo_dirs):
        """When ha_ws_client is omitted, executor._ws_client stays None."""
        MockLoop = self._run(tmp_path)
        executor = MockLoop.call_args.kwargs["tool_executor"]
        assert executor._ws_client is None


class TestRepairIssuePlaceholderPreInject:
    """_run_repair_issue_investigation pre-injects translation_placeholders,
    learn_more_url, and is_fixable from HARepairIssue.data into initial_context."""

    def test_placeholders_in_initial_context(self, tmp_path, pueo_dirs):
        import asyncio
        from unittest import mock
        from unittest.mock import AsyncMock, MagicMock

        from utils.agent.tool_registry import AgentLoopResult

        db_path = _make_repair_db(tmp_path)
        fake_result = AgentLoopResult(outcome="success")
        captured: list[str] = []

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

            async def _capture(**kw):
                captured.append(kw.get("initial_context", ""))
                return fake_result

            mock_instance.run = _capture  # type: ignore[method-assign]

            asyncio.run(
                _run_repair_issue_investigation(
                    issue=_FakeRepairIssueWS(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )

        assert captured, "AgentLoop.run must have been called"
        ctx = captured[0]
        assert (
            "Attic, Basement" in ctx
        ), "translation_placeholders must appear in context"
        assert "Learn more" in ctx, "learn_more_url must appear in context"
        assert "Is fixable" in ctx, "is_fixable must appear in context"


# ---------------------------------------------------------------------------
# Replay protection (#975)
# ---------------------------------------------------------------------------

_TS_LINE = (
    "\x1b[31m2026-10-22 07:22:46.863 ERROR (paho) [root] Uncaught thread exception"
)
_TS_LINE_SAME_TS = "\x1b[31m2026-10-22 07:22:46.863 ERROR (Main) [x] Error doing job"
_TS_LINE_LATER = "\x1b[31m2026-10-22 07:30:00.000 ERROR (Main) [y] Component error"


class TestStripAnsi:
    def test_removes_colour_codes(self, pueo_dirs):
        from agents.ha_log_monitor import _strip_ansi

        assert _strip_ansi("\x1b[31mhello\x1b[0m") == "hello"

    def test_plain_line_unchanged(self, pueo_dirs):
        from agents.ha_log_monitor import _strip_ansi

        assert _strip_ansi("plain") == "plain"


class TestParseHaLogTs:
    def test_valid_with_millis(self, pueo_dirs):
        import datetime

        from agents.ha_log_monitor import _parse_ha_log_ts

        expected = datetime.datetime(2026, 10, 22, 7, 22, 46, 863000).timestamp()
        assert _parse_ha_log_ts("2026-10-22 07:22:46.863 ERROR x") == expected

    def test_valid_without_millis(self, pueo_dirs):
        import datetime

        from agents.ha_log_monitor import _parse_ha_log_ts

        expected = datetime.datetime(2026, 10, 22, 7, 22, 46).timestamp()
        assert _parse_ha_log_ts("2026-10-22 07:22:46 INFO x") == expected

    def test_ansi_wrapped(self, pueo_dirs):
        from agents.ha_log_monitor import _parse_ha_log_ts

        assert _parse_ha_log_ts(_TS_LINE) == _parse_ha_log_ts(
            "2026-10-22 07:22:46.863 ERROR"
        )

    def test_continuation_line_returns_none(self, pueo_dirs):
        from agents.ha_log_monitor import _parse_ha_log_ts

        assert _parse_ha_log_ts('  File "/usr/lib/x.py", line 3, in run') is None
        assert _parse_ha_log_ts("Traceback (most recent call last):") is None

    def test_garbage_returns_none(self, pueo_dirs):
        from agents.ha_log_monitor import _parse_ha_log_ts

        assert _parse_ha_log_ts("") is None
        assert _parse_ha_log_ts("2026-13-45 99:99:99.000 ERROR") is None


class TestReplayGate:
    def _state(self):
        from agents.ha_log_monitor import _LogStreamState

        return _LogStreamState(loop_name="t")

    def test_no_timestamp_passes(self, pueo_dirs):
        from agents.ha_log_monitor import _replay_gate

        state = self._state()
        assert _replay_gate(state, "fp") is True
        assert _replay_gate(state, "fp") is True
        assert state.watermark is None

    def test_first_line_sets_watermark(self, pueo_dirs):
        from agents.ha_log_monitor import _replay_gate

        state = self._state()
        state.entry_ts = 100.0
        assert _replay_gate(state, "a") is True
        assert state.watermark == 100.0
        assert state.seen_at_watermark == {"a"}

    def test_older_line_skipped(self, pueo_dirs):
        from agents.ha_log_monitor import _replay_gate

        state = self._state()
        state.watermark = 100.0
        state.entry_ts = 99.0
        assert _replay_gate(state, "a") is False

    def test_same_ts_seen_skipped_unseen_passes(self, pueo_dirs):
        from agents.ha_log_monitor import _replay_gate

        state = self._state()
        state.watermark = 100.0
        state.seen_at_watermark = {"a"}
        state.entry_ts = 100.0
        assert _replay_gate(state, "a") is False
        assert _replay_gate(state, "b") is True
        assert state.seen_at_watermark == {"a", "b"}

    def test_newer_line_resets_seen_set(self, pueo_dirs):
        from agents.ha_log_monitor import _replay_gate

        state = self._state()
        state.watermark = 100.0
        state.seen_at_watermark = {"a", "b"}
        state.entry_ts = 101.0
        assert _replay_gate(state, "c") is True
        assert state.watermark == 101.0
        assert state.seen_at_watermark == {"c"}


def _make_prefs_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "prefs.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    return db_path


class TestWatermarkPersistence:
    def test_save_then_load_round_trip(self, pueo_dirs, tmp_path, monkeypatch):
        import config
        from agents.ha_log_monitor import _load_watermark, _save_watermark

        monkeypatch.setattr(config, "DB_PATH", _make_prefs_db(tmp_path))
        _save_watermark("core", 123.5, ["a", "b"])
        _save_watermark("core", 124.0, ["c"])
        assert _load_watermark("core") == (124.0, {"c"})
        assert _load_watermark("supervisor") is None

    def test_missing_table_fails_open(self, pueo_dirs, tmp_path, monkeypatch):
        import config
        from agents.ha_log_monitor import _load_watermark, _save_watermark

        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "empty.db"))
        _save_watermark("core", 1.0, [])
        assert _load_watermark("core") is None

    def test_restore_adopts_newer_only(self, pueo_dirs, tmp_path, monkeypatch):
        import config
        from agents.ha_log_monitor import (
            _LogStreamState,
            _restore_watermark,
            _save_watermark,
        )

        monkeypatch.setattr(config, "DB_PATH", _make_prefs_db(tmp_path))
        state = _LogStreamState(loop_name="t")
        _restore_watermark(state, "core")  # nothing persisted
        assert state.watermark is None

        _save_watermark("core", 100.0, ["a"])
        _restore_watermark(state, "core")
        assert (state.watermark, state.seen_at_watermark) == (100.0, {"a"})

        state.watermark, state.seen_at_watermark = 200.0, {"z"}
        _restore_watermark(state, "core")
        assert (state.watermark, state.seen_at_watermark) == (200.0, {"z"})


class TestTailStreamReplay:
    """Reconnects replay the backlog; already-triaged lines must not re-triage."""

    @pytest.fixture
    def stream_env(self, pueo_dirs, tmp_path, monkeypatch):
        import config
        from agents import ha_log_monitor
        from agents.ha_log_monitor import LogEvaluation, _LogStreamState
        from utils.llm.ollama_client import FakeLLMClient

        monkeypatch.setattr(config, "DB_PATH", _make_prefs_db(tmp_path))
        monkeypatch.setattr(
            ha_log_monitor, "_core_state", _LogStreamState(loop_name="ha_log_monitor")
        )
        monkeypatch.setattr(ha_log_monitor, "_init_sparkline_db", lambda s: None)
        monkeypatch.setattr(ha_log_monitor, "_record_sparkline_line", lambda s, m: None)
        r = LogEvaluation(
            is_actionable=False, root_cause_summary="x", confidence_score=0.1
        )
        return FakeLLMClient(r.model_dump_json())

    def _run(self, lines, llm):
        import asyncio

        from agents.ha_log_monitor import tail_remote_log_stream
        from utils.ha.ssh_client import FakeSSHClient

        asyncio.run(
            tail_remote_log_stream(
                ssh_client=FakeSSHClient(stream_data=lines), llm_client=llm
            )
        )

    def test_replayed_backlog_triaged_once(self, stream_env):
        lines = [_TS_LINE, '  File "x.py", line 1', "INFO ok"]
        self._run(lines, stream_env)
        self._run(lines, stream_env)
        assert len(stream_env.calls) == 1

    def test_equal_timestamp_distinct_lines_both_triaged(self, stream_env):
        lines = [_TS_LINE, _TS_LINE_SAME_TS]
        self._run(lines, stream_env)
        self._run(lines, stream_env)
        assert len(stream_env.calls) == 2

    def test_new_line_after_replay_is_triaged(self, stream_env):
        self._run([_TS_LINE], stream_env)
        self._run([_TS_LINE, _TS_LINE_LATER], stream_env)
        assert len(stream_env.calls) == 2

    def test_traceback_header_inherits_entry_ts(self, stream_env):
        lines = [
            "\x1b[31m2026-10-22 07:22:46.863 ERROR (Main) [x] boom",
            "Traceback (most recent call last):",
        ]
        self._run(lines, stream_env)
        self._run(lines, stream_env)
        assert len(stream_env.calls) == 1

    def test_watermark_survives_restart(self, stream_env, monkeypatch):
        from agents import ha_log_monitor
        from agents.ha_log_monitor import _LogStreamState

        self._run([_TS_LINE], stream_env)
        # Simulate a Pueo restart: fresh in-memory state, same DB.
        monkeypatch.setattr(
            ha_log_monitor, "_core_state", _LogStreamState(loop_name="ha_log_monitor")
        )
        self._run([_TS_LINE], stream_env)
        assert len(stream_env.calls) == 1

    def test_buffer_is_per_source(self, stream_env, monkeypatch):
        from agents import ha_log_monitor
        from agents.ha_log_monitor import _LogStreamState

        sup = _LogStreamState(loop_name="ha_log_monitor_supervisor")
        monkeypatch.setattr(ha_log_monitor, "_supervisor_state", sup)
        self._run(["INFO core line"], stream_env)
        assert list(ha_log_monitor._core_state.log_buffer) == ["INFO core line"]
        assert list(sup.log_buffer) == []


# ---------------------------------------------------------------------------
# TestRecurrenceFingerprint
# ---------------------------------------------------------------------------


class TestRecurrenceFingerprint:
    def test_uses_exception_line_over_header(self, pueo_dirs):
        from agents.ha_log_monitor import _recurrence_fingerprint

        block = [
            "2026-01-01 12:00:00 ERROR [homeassistant.sensors] Something went wrong",
            "Traceback (most recent call last):",
            "  File 'foo.py', line 42, in bar",
            "COOPSAPIError: Invalid datum value 42",
        ]
        fp = _recurrence_fingerprint(block)
        # Should contain the exception class, not the header
        assert "COOPSAPIError" in fp
        assert "Traceback" not in fp

    def test_digits_normalised(self, pueo_dirs):
        from agents.ha_log_monitor import _recurrence_fingerprint

        block1 = ["SomeError: failed to connect to port 8123"]
        block2 = ["SomeError: failed to connect to port 9999"]
        assert _recurrence_fingerprint(block1) == _recurrence_fingerprint(block2)

    def test_falls_back_to_first_line(self, pueo_dirs):
        from agents.ha_log_monitor import _recurrence_fingerprint

        block = ["2026-01-01 12:00:00 ERROR plain message 99"]
        fp = _recurrence_fingerprint(block)
        assert "plain message N" in fp

    def test_recurrence_window_is_7_days(self, pueo_dirs):
        from agents.ha_log_monitor import _LOG_RECURRENCE_WINDOW

        assert _LOG_RECURRENCE_WINDOW == 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Spook digest dedup fix — issue #1089
# ---------------------------------------------------------------------------


def _make_digest_db(tmp_path: Path, *, with_sent_digest: bool = False) -> str:
    """Create a test DB, optionally pre-seeding a sent Spook digest row."""
    db_path = str(tmp_path / "digest_test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(_HITL_DDL)
        conn.execute(_HA_REPAIR_DDL)
        if with_sent_digest:
            now = time.time()
            conn.execute(
                "INSERT INTO ha_repair_history"
                " (issue_key, translation_key, domain,"
                "  first_seen_at, last_seen_at, hitl_sent_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "spook/digest:empty_areas",
                    "empty_areas",
                    "spook",
                    now - 100,
                    now - 50,
                    now - 50,
                ),
            )
    return db_path


class _FakeSpookIssue:
    """Minimal stand-in for HARepairIssue with a Spook domain."""

    def __init__(self, tkey: str = "empty_areas", issue_id: str = "") -> None:
        _id = issue_id or f"spook-issue-uuid-{tkey}"
        self.issue_key = _id
        self.issue_id = _id
        self.domain = "spook"
        self.severity = "warning"
        self.translation_key = tkey
        self.breaks_in_ha_version = None
        self.data: dict = {}


def _run_one_repair_poll(
    db_path: str,
    spook_issues: list,
    *,
    monkeypatch: Any,
    resolved_calls: list,
    verify_calls: list,
) -> None:
    """Run exactly one iteration of poll_for_repairs with the given issue list.

    Mocks out every external dependency so no network or real DB access occurs.
    resolved_calls and verify_calls are populated by the mock side-effects.
    """
    # supervised_sleep: return immediately on first call, cancel on second
    _sleep_count = [0]

    async def _fake_sleep(*a: Any, **kw: Any) -> None:
        _sleep_count[0] += 1
        if _sleep_count[0] > 1:
            raise asyncio.CancelledError()

    # get_ha_repair_issues: return the provided list
    async def _fake_get_issues(_client: Any) -> list:
        return list(spook_issues)

    # mark_repair_resolved: record the key and update the DB
    def _fake_mark_resolved(key: str) -> None:
        resolved_calls.append(key)
        with sqlite3.connect(db_path) as _c:
            _c.execute(
                "UPDATE ha_repair_history SET resolved_at = ? WHERE issue_key = ?",
                (time.time(), key),
            )

    # resolve_session_outcome: record and no-op (no session_outcomes table)
    def _fake_resolve_session(key: str, verification: str, by: str, **kw: Any) -> None:
        verify_calls.append(key)

    # record_repair_seen: no-op to avoid needing the full schema
    def _fake_record_seen(*a: Any, **kw: Any) -> None:
        pass

    with (
        mock.patch(
            "utils.agent.supervisor.supervised_sleep",
            side_effect=_fake_sleep,
        ),
        mock.patch(
            "utils.ha.ha_rest_client.get_ha_repair_issues",
            side_effect=_fake_get_issues,
        ),
        mock.patch(
            "agents.ha_agent_advanced.mark_repair_resolved",
            side_effect=_fake_mark_resolved,
        ),
        mock.patch(
            "agents.ha_agent_advanced.resolve_session_outcome",
            side_effect=_fake_resolve_session,
        ),
        mock.patch(
            "agents.ha_agent_advanced.record_repair_seen",
            side_effect=_fake_record_seen,
        ),
        mock.patch(
            "utils.agent.work_queue.get_work_queue_or_none",
            return_value=None,
        ),
        mock.patch(
            "agents.ha_log_monitor._run_spook_digest_investigation",
            new=AsyncMock(),
        ),
    ):
        from utils.hitl.notify import FakeNotifier

        try:
            asyncio.run(
                __import__(
                    "agents.ha_log_monitor", fromlist=["poll_for_repairs"]
                ).poll_for_repairs(
                    ha_ws_client=MagicMock(),
                    ha_rest_client=MagicMock(),
                    notifier=FakeNotifier(),
                    db_path=db_path,
                )
            )
        except asyncio.CancelledError:
            pass


class TestSpookDigestDedup:
    """Regression tests for the Spook digest false-resolve loop (issue #1089).

    Before the fix, a sent digest key was absent from active_keys on the second
    poll (because _spook_unsent_by_tkey was empty once all issues were sent),
    causing the reconcile to falsely mark the digest resolved every poll.
    """

    def test_live_digest_not_resolved_on_second_poll(self, tmp_path, pueo_dirs):
        """A sent digest whose underlying Spook issues are still live must not be resolved."""
        db_path = _make_digest_db(tmp_path, with_sent_digest=True)
        resolved: list = []
        verify: list = []

        _run_one_repair_poll(
            db_path,
            [_FakeSpookIssue("empty_areas")],
            monkeypatch=None,
            resolved_calls=resolved,
            verify_calls=verify,
        )

        assert (
            "spook/digest:empty_areas" not in resolved
        ), "digest must not be falsely resolved while its Spook issue is still live"
        assert (
            "spook/digest:empty_areas" not in verify
        ), "no verification must be recorded while the digest is still live"

    def test_digest_resolved_when_issues_gone(self, tmp_path, pueo_dirs):
        """When no Spook issues exist, a previously-sent digest must be resolved."""
        db_path = _make_digest_db(tmp_path, with_sent_digest=True)
        resolved: list = []
        verify: list = []

        _run_one_repair_poll(
            db_path,
            [],  # all issues gone
            monkeypatch=None,
            resolved_calls=resolved,
            verify_calls=verify,
        )

        assert (
            "spook/digest:empty_areas" in resolved
        ), "digest must be resolved once all its underlying Spook issues are gone"


class TestSpookDigestPayload:
    """Unit tests for build_spook_digest_payload (utils/ha/spook_digest.py)."""

    def test_known_key_returns_payload(self):
        from utils.ha.spook_digest import build_spook_digest_payload

        issue = _FakeSpookIssue(tkey="empty_areas", issue_id="spook/empty_areas-1")
        issue.data = {
            "translation_placeholders": {"area": "Aiden's Room"},
            "is_fixable": True,
            "learn_more_url": "https://spook.boo/repairs/empty_areas",
        }
        payload = build_spook_digest_payload("empty_areas", [issue])

        assert payload is not None
        assert payload["card_type"] == "spook_digest"
        assert payload["translation_key"] == "empty_areas"
        assert len(payload["items"]) == 1
        assert payload["items"][0]["label"] == "Aiden's Room"
        assert payload["items"][0]["is_fixable"] is True
        assert (
            payload["items"][0]["learn_more_url"]
            == "https://spook.boo/repairs/empty_areas"
        )

    def test_unknown_key_returns_none(self):
        from utils.ha.spook_digest import build_spook_digest_payload

        issue = _FakeSpookIssue(tkey="unknown_engines")
        payload = build_spook_digest_payload("unknown_engines", [issue])

        assert payload is None, "unknown_engines must go through the LLM path"

    def test_missing_placeholders_falls_back_to_issue_id(self):
        from utils.ha.spook_digest import build_spook_digest_payload

        issue = _FakeSpookIssue(tkey="unused_labels", issue_id="spook/unused_labels-7")
        issue.data = {}  # no translation_placeholders
        payload = build_spook_digest_payload("unused_labels", [issue])

        assert payload is not None
        assert payload["items"][0]["label"] == "spook/unused_labels-7"

    def test_all_known_keys_accepted(self):
        from utils.ha.spook_digest import KNOWN_CLEANUP_KEYS, build_spook_digest_payload

        for tkey in KNOWN_CLEANUP_KEYS:
            issue = _FakeSpookIssue(tkey=tkey)
            issue.data = {}
            payload = build_spook_digest_payload(tkey, [issue])
            assert payload is not None, f"expected payload for known key: {tkey}"

    def test_multiple_issues_all_in_items(self):
        from utils.ha.spook_digest import build_spook_digest_payload

        issues = []
        for i in range(3):
            iss = _FakeSpookIssue(tkey="unused_blueprints", issue_id=f"bp-{i}")
            iss.data = {
                "translation_placeholders": {"blueprint": f"Blueprint {i}"},
                "is_fixable": True,
            }
            issues.append(iss)
        payload = build_spook_digest_payload("unused_blueprints", issues)

        assert payload is not None
        assert len(payload["items"]) == 3
        assert payload["items"][1]["label"] == "Blueprint 1"

    def test_suppression_key_and_notification_id_stable(self):
        from utils.ha.spook_digest import build_spook_digest_payload
        from utils.hitl.hitl_tracker import stable_nid

        issue = _FakeSpookIssue(tkey="orphaned_statistics")
        issue.data = {}
        payload = build_spook_digest_payload("orphaned_statistics", [issue])

        assert payload is not None
        assert payload["suppression_key"] == "spook/digest:orphaned_statistics"
        assert payload["notification_id"] == stable_nid(
            "spook/digest:orphaned_statistics"
        )

    def test_body_includes_items(self):
        from utils.ha.spook_digest import build_spook_digest_payload

        issue = _FakeSpookIssue(tkey="empty_areas")
        issue.data = {
            "translation_placeholders": {"area": "Upstairs"},
            "is_fixable": True,
        }
        payload = build_spook_digest_payload("empty_areas", [issue])

        assert payload is not None
        assert "empty_areas" in payload["body"]
        assert "Upstairs" in payload["body"]

    def test_known_keys_have_fix_metadata(self):
        """Every known cleanup key must carry fix_label, fix_effect and menu_choice."""
        from utils.ha.spook_digest import KNOWN_CLEANUP_KEYS, build_spook_digest_payload

        for tkey in KNOWN_CLEANUP_KEYS:
            issue = _FakeSpookIssue(tkey=tkey)
            issue.data = {}
            payload = build_spook_digest_payload(tkey, [issue])
            assert payload is not None
            item = payload["items"][0]
            assert item.get("fix_label"), f"{tkey}: expected non-empty fix_label"
            assert item.get("fix_effect"), f"{tkey}: expected non-empty fix_effect"
            assert (
                item.get("menu_choice") == "remove"
            ), f"{tkey}: expected menu_choice='remove'"

    def test_empty_areas_fix_label_and_effect(self):
        from utils.ha.spook_digest import build_spook_digest_payload

        issue = _FakeSpookIssue(tkey="empty_areas", issue_id="ea-1")
        issue.data = {"translation_placeholders": {"area": "Aiden's Room"}}
        payload = build_spook_digest_payload("empty_areas", [issue])

        assert payload is not None
        item = payload["items"][0]
        assert item["fix_label"] == "Delete area"
        assert "area" in item["fix_effect"].lower()
        assert item["menu_choice"] == "remove"

    def test_orphaned_statistics_effect_mentions_irreversible(self):
        from utils.ha.spook_digest import build_spook_digest_payload

        issue = _FakeSpookIssue(tkey="orphaned_statistics", issue_id="stat-1")
        issue.data = {}
        payload = build_spook_digest_payload("orphaned_statistics", [issue])

        assert payload is not None
        item = payload["items"][0]
        assert item["fix_label"] == "Clear statistics"
        assert "backup" in item["fix_effect"].lower()


class TestSpookDigestHybridRouting:
    """Known Spook cleanup keys must bypass the LLM work queue in poll_for_repairs."""

    def test_known_key_sends_deterministic_card_not_llm(self, tmp_path, pueo_dirs):
        """empty_areas is a known cleanup key — it must not trigger _run_spook_digest_investigation."""
        db_path = _make_digest_db(tmp_path, with_sent_digest=False)

        llm_investigations: list = []
        sent_cards: list = []
        mark_sent_calls: list = []

        async def _fake_get_issues(_client: Any) -> list:
            issue = _FakeSpookIssue(tkey="empty_areas")
            issue.data = {
                "translation_placeholders": {"area": "Aiden's Room"},
                "is_fixable": True,
            }
            return [issue]

        async def _fake_send(*, subject: str, body: str, payload: dict) -> None:
            sent_cards.append(payload)

        def _fake_mark_sent(key: str) -> None:
            mark_sent_calls.append(key)

        def _fake_record_seen(*a: Any, **kw: Any) -> None:
            pass

        def _fake_resolve(*a: Any, **kw: Any) -> None:
            pass

        _sleep_count = [0]

        async def _fake_sleep(*a: Any, **kw: Any) -> None:
            _sleep_count[0] += 1
            if _sleep_count[0] > 1:
                raise asyncio.CancelledError()

        with (
            mock.patch(
                "utils.agent.supervisor.supervised_sleep", side_effect=_fake_sleep
            ),
            mock.patch(
                "utils.ha.ha_rest_client.get_ha_repair_issues",
                side_effect=_fake_get_issues,
            ),
            mock.patch(
                "agents.ha_agent_advanced.mark_repair_resolved",
                side_effect=_fake_resolve,
            ),
            mock.patch(
                "agents.ha_agent_advanced.resolve_session_outcome",
                side_effect=_fake_resolve,
            ),
            mock.patch(
                "agents.ha_agent_advanced.record_repair_seen",
                side_effect=_fake_record_seen,
            ),
            mock.patch(
                "agents.ha_agent_advanced.mark_repair_hitl_sent",
                side_effect=_fake_mark_sent,
            ),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
            mock.patch(
                "agents.ha_log_monitor._run_spook_digest_investigation",
                side_effect=lambda *a, **kw: llm_investigations.append(kw),
            ),
        ):
            from utils.hitl.notify import FakeNotifier

            notifier = FakeNotifier()
            notifier.send = _fake_send  # type: ignore[method-assign]
            try:
                asyncio.run(
                    __import__(
                        "agents.ha_log_monitor", fromlist=["poll_for_repairs"]
                    ).poll_for_repairs(
                        ha_ws_client=MagicMock(),
                        ha_rest_client=MagicMock(),
                        notifier=notifier,
                        db_path=db_path,
                    )
                )
            except asyncio.CancelledError:
                pass

        assert llm_investigations == [], "known key must NOT trigger LLM investigation"
        assert (
            len(sent_cards) == 1
        ), "known key must send exactly one deterministic card"
        assert sent_cards[0]["card_type"] == "spook_digest"
        assert sent_cards[0]["translation_key"] == "empty_areas"
        assert len(mark_sent_calls) == 1
        assert mark_sent_calls[0] == "spook/digest:empty_areas"

    def test_unknown_key_routes_to_llm(self, tmp_path, pueo_dirs):
        """unknown_engines is not a known cleanup key — it must trigger LLM investigation."""
        db_path = _make_digest_db(tmp_path, with_sent_digest=False)

        llm_investigations: list = []
        sent_cards: list = []

        async def _fake_get_issues(_client: Any) -> list:
            issue = _FakeSpookIssue(tkey="unknown_engines")
            issue.data = {}
            return [issue]

        async def _fake_send(*, subject: str, body: str, payload: dict) -> None:
            sent_cards.append(payload)

        def _fake_record_seen(*a: Any, **kw: Any) -> None:
            pass

        def _fake_resolve(*a: Any, **kw: Any) -> None:
            pass

        _sleep_count = [0]

        async def _fake_sleep(*a: Any, **kw: Any) -> None:
            _sleep_count[0] += 1
            if _sleep_count[0] > 1:
                raise asyncio.CancelledError()

        with (
            mock.patch(
                "utils.agent.supervisor.supervised_sleep", side_effect=_fake_sleep
            ),
            mock.patch(
                "utils.ha.ha_rest_client.get_ha_repair_issues",
                side_effect=_fake_get_issues,
            ),
            mock.patch(
                "agents.ha_agent_advanced.mark_repair_resolved",
                side_effect=_fake_resolve,
            ),
            mock.patch(
                "agents.ha_agent_advanced.resolve_session_outcome",
                side_effect=_fake_resolve,
            ),
            mock.patch(
                "agents.ha_agent_advanced.record_repair_seen",
                side_effect=_fake_record_seen,
            ),
            mock.patch(
                "utils.agent.work_queue.get_work_queue_or_none", return_value=None
            ),
            mock.patch(
                "agents.ha_log_monitor._run_spook_digest_investigation",
                new=AsyncMock(
                    side_effect=lambda *a, **kw: llm_investigations.append(kw)
                ),
            ),
        ):
            from utils.hitl.notify import FakeNotifier

            notifier = FakeNotifier()
            notifier.send = _fake_send  # type: ignore[method-assign]
            try:
                asyncio.run(
                    __import__(
                        "agents.ha_log_monitor", fromlist=["poll_for_repairs"]
                    ).poll_for_repairs(
                        ha_ws_client=MagicMock(),
                        ha_rest_client=MagicMock(),
                        notifier=notifier,
                        db_path=db_path,
                    )
                )
            except asyncio.CancelledError:
                pass

        assert (
            len(llm_investigations) == 1
        ), "unknown key must trigger LLM investigation"
        assert sent_cards == [], "unknown key must NOT send a deterministic card"


# ---------------------------------------------------------------------------
# S6: Custom-integration tracebacks — advise-only integration bug card
# ---------------------------------------------------------------------------


class TestExtractCustomComponentInfo:
    """Pure-function tests for _extract_custom_component_info."""

    def test_spook_frame(self, pueo_dirs):
        from agents.ha_log_monitor import _extract_custom_component_info

        block = [
            "2026-11-03 21:15:00 ERROR (MainThread) [spook] Error doing job",
            "Traceback (most recent call last):",
            '  File "/config/custom_components/spook/ectoplasms/homeassistant/sensor.py", line 291, in async_update',
            "KeyError: 'custom_components'",
        ]
        result = _extract_custom_component_info(block)
        assert result is not None
        domain, file_line = result
        assert domain == "spook"
        assert file_line == "ectoplasms/homeassistant/sensor.py:291"

    def test_non_custom_component_returns_none(self, pueo_dirs):
        from agents.ha_log_monitor import _extract_custom_component_info

        block = [
            "2026-11-03 ERROR Something broke",
            '  File "/usr/local/lib/python3.12/site-packages/homeassistant/components/sensor.py", line 42, in update',
            "AttributeError: some error",
        ]
        assert _extract_custom_component_info(block) is None

    def test_empty_block_returns_none(self, pueo_dirs):
        from agents.ha_log_monitor import _extract_custom_component_info

        assert _extract_custom_component_info([]) is None

    def test_uses_first_matching_frame(self, pueo_dirs):
        from agents.ha_log_monitor import _extract_custom_component_info

        block = [
            '  File "/config/custom_components/domain_a/file.py", line 10, in foo',
            '  File "/config/custom_components/domain_b/file.py", line 20, in bar',
        ]
        result = _extract_custom_component_info(block)
        assert result is not None
        assert result[0] == "domain_a"
        assert result[1] == "file.py:10"

    def test_longer_path_prefix(self, pueo_dirs):
        from agents.ha_log_monitor import _extract_custom_component_info

        block = [
            '  File "/homeassistant/config/custom_components/myint/sensor.py", line 5, in update',
        ]
        result = _extract_custom_component_info(block)
        assert result is not None
        assert result[0] == "myint"
        assert result[1] == "sensor.py:5"


class TestIntegrationBugRouting:
    """Custom-component tracebacks must route to advise-only card, not repair pipeline."""

    @pytest.fixture
    def routing_env(self, pueo_dirs, tmp_path, monkeypatch):
        import config
        from agents import ha_log_monitor
        from agents.ha_log_monitor import LogEvaluation, _LogStreamState

        monkeypatch.setattr(config, "DB_PATH", _make_prefs_db(tmp_path))
        monkeypatch.setattr(
            ha_log_monitor,
            "_core_state",
            _LogStreamState(loop_name="ha_log_monitor"),
        )
        monkeypatch.setattr(ha_log_monitor, "_init_sparkline_db", lambda s: None)
        monkeypatch.setattr(ha_log_monitor, "_record_sparkline_line", lambda s, m: None)

        return LogEvaluation(
            is_actionable=True,
            root_cause_summary="Spook sensor platform crashed",
            confidence_score=0.9,
        )

    def test_custom_component_bypasses_repair_pipeline(self, routing_env, monkeypatch):
        """An actionable traceback in a custom component sends the advise-only card and does not trigger the repair pipeline."""
        from agents import ha_log_monitor
        from agents.ha_log_monitor import tail_remote_log_stream
        from utils.ha.ssh_client import FakeSSHClient
        from utils.llm.ollama_client import FakeLLMClient

        bug_card_calls: list[dict] = []
        repair_pipeline_calls: list[bool] = []

        async def _fake_bug_card(
            domain: str,
            file_line: str,
            exc_class: Any,
            client: Any,
            notifier: Any,
        ) -> None:
            bug_card_calls.append({"domain": domain, "file_line": file_line})

        async def _fake_repair() -> None:
            repair_pipeline_calls.append(True)

        monkeypatch.setattr(
            ha_log_monitor, "_send_integration_bug_card", _fake_bug_card
        )
        monkeypatch.setattr(
            ha_log_monitor, "trigger_remediation_pipeline", _fake_repair
        )

        llm = FakeLLMClient(routing_env.model_dump_json())
        lines = [
            "2026-11-03 21:15:00.000 ERROR (MainThread) [custom_components.spook] Error doing job",
            "Traceback (most recent call last):",
            '  File "/config/custom_components/spook/ectoplasms/homeassistant/sensor.py", line 291, in async_update',
            "KeyError: 'custom_components'",
        ]

        with mock.patch(
            "utils.agent.work_queue.get_work_queue_or_none", return_value=None
        ):
            asyncio.run(
                tail_remote_log_stream(
                    ssh_client=FakeSSHClient(stream_data=lines),
                    llm_client=llm,
                )
            )

        assert len(bug_card_calls) == 1, "must send exactly one integration-bug card"
        assert bug_card_calls[0]["domain"] == "spook"
        assert "sensor.py:291" in bug_card_calls[0]["file_line"]
        assert repair_pipeline_calls == [], "repair pipeline must NOT be triggered"


# ---------------------------------------------------------------------------
# _hacs_repo_id_from_issue — marketplace domain support
# ---------------------------------------------------------------------------


class TestHacsRepoIdFromIssue:
    """_hacs_repo_id_from_issue handles both hacs and marketplace domains."""

    def _make_issue(self, domain, issue_id):
        from utils.ha.ha_rest_client import HARepairIssue

        return HARepairIssue(
            domain=domain,
            issue_id=issue_id,
            severity="warning",
            issue_key=f"{domain}/{issue_id}",
            breaks_in_ha_version=None,
            translation_key="restart_required",
        )

    def test_hacs_domain_extracts_repo_id(self):
        from agents.ha_log_monitor import _hacs_repo_id_from_issue

        issue = self._make_issue("hacs", "restart_required_42_some_suffix")
        assert _hacs_repo_id_from_issue(issue) == "42"

    def test_marketplace_domain_extracts_repo_id(self):
        from agents.ha_log_monitor import _hacs_repo_id_from_issue

        issue = self._make_issue("marketplace", "restart_required_12345_v1")
        assert _hacs_repo_id_from_issue(issue) == "12345"

    def test_other_domain_returns_empty(self):
        from agents.ha_log_monitor import _hacs_repo_id_from_issue

        issue = self._make_issue("homeassistant", "restart_required_99")
        assert _hacs_repo_id_from_issue(issue) == ""

    def test_no_numeric_id_returns_empty(self):
        from agents.ha_log_monitor import _hacs_repo_id_from_issue

        issue = self._make_issue("marketplace", "restart_required_no_number_here")
        assert _hacs_repo_id_from_issue(issue) == ""
