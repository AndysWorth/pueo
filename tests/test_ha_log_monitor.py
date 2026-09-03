"""Unit tests for ha_log_monitor — _LogStreamState, source routing, sparkline helpers."""

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
