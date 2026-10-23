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
