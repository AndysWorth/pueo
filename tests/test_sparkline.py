"""Tests for sparkline min_line_ts aggregation (V27 migration)."""

import sqlite3
import time

import pytest


def _create_stream_metrics_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE stream_metrics ("
        "loop_name TEXT NOT NULL, "
        "bucket_ts INTEGER NOT NULL, "
        "total_lines INTEGER NOT NULL DEFAULT 0, "
        "match_lines INTEGER NOT NULL DEFAULT 0, "
        "min_line_ts INTEGER, "
        "PRIMARY KEY (loop_name, bucket_ts)"
        ")"
    )
    conn.commit()


class TestHALogSparklineMinLineTs:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import agents.ha_log_monitor as mod
        import config as _config

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(mod, "_config", type("_C", (), {"DB_PATH": path})())
        with sqlite3.connect(path) as conn:
            _create_stream_metrics_table(conn)
        return path

    def _seed(self, db_path, loop_name, bucket_ts, total, matches, min_line_ts=None):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO stream_metrics "
                "(loop_name, bucket_ts, total_lines, match_lines, min_line_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (loop_name, bucket_ts, total, matches, min_line_ts),
            )

    def test_min_line_ts_used_as_display_ts(self, db_path, monkeypatch):
        import agents.ha_log_monitor as mod

        now = int(time.time())
        bucket_ts = (now // 60) * 60 - 120  # 2 minutes ago
        actual_first_line = bucket_ts + 5  # first line was 5s into the bucket
        self._seed(db_path, mod._LOOP_NAME, bucket_ts, 10, 2, actual_first_line)

        # Suppress the in-progress bucket from affecting the test
        mod._core_state.bucket_total = 0
        mod._core_state.bucket_matches = 0
        mod._core_state.bucket_min_ts = 0
        mod._core_state.current_minute = now // 60

        result = mod.get_ha_log_sparkline_data(bucket_size="1m", time_range="1h")
        past = [b for b in result["buckets"] if b[0] == actual_first_line]
        assert past, "bucket with min_line_ts display_ts not found"
        assert past[0][1] == 10
        assert past[0][2] == 2

    def test_null_min_line_ts_falls_back_to_bucket_ts(self, db_path, monkeypatch):
        import agents.ha_log_monitor as mod

        now = int(time.time())
        bucket_ts = (now // 60) * 60 - 120
        self._seed(db_path, mod._LOOP_NAME, bucket_ts, 7, 1, None)

        mod._core_state.bucket_total = 0
        mod._core_state.bucket_matches = 0
        mod._core_state.bucket_min_ts = 0
        mod._core_state.current_minute = now // 60

        result = mod.get_ha_log_sparkline_data(bucket_size="1m", time_range="1h")
        past = [b for b in result["buckets"] if b[0] == bucket_ts]
        assert past, "bucket with fallback bucket_ts not found"
        assert past[0][1] == 7

    def test_aggregation_uses_min_across_buckets(self, db_path, monkeypatch):
        """Three 1m rows in the same hour: display_ts must equal the smallest min_line_ts."""
        import agents.ha_log_monitor as mod

        now = int(time.time())
        hour_floor = (now // 3600) * 3600 - 7200  # 2 hours ago
        ts_a = hour_floor + 0  # first 1m bucket in that hour
        ts_b = hour_floor + 60  # second
        ts_c = hour_floor + 120  # third
        min_a = ts_a + 3
        min_b = ts_b + 7
        min_c = ts_c + 1
        for ts, total, mts in [(ts_a, 5, min_a), (ts_b, 3, min_b), (ts_c, 8, min_c)]:
            self._seed(db_path, mod._LOOP_NAME, ts, total, 0, mts)

        mod._core_state.bucket_total = 0
        mod._core_state.bucket_matches = 0
        mod._core_state.bucket_min_ts = 0
        mod._core_state.current_minute = now // 60

        result = mod.get_ha_log_sparkline_data(bucket_size="1h", time_range="6h")
        # All three 1m rows collapse into a single 1h bucket
        # display_ts should be min(min_a, min_b, min_c) = min_c = ts_c + 1
        expected_display = min(min_a, min_b, min_c)
        matching = [b for b in result["buckets"] if b[0] == expected_display]
        assert (
            matching
        ), f"expected display_ts {expected_display}, got {[b[0] for b in result['buckets']]}"
        assert matching[0][1] == 16  # 5 + 3 + 8


class TestNAXSparklineMinLineTs:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        import netalertx.log_monitor as mod

        path = str(tmp_path / "test_nax.db")
        monkeypatch.setattr(
            mod, "_config", type("_C", (), {"DB_PATH": path, "NETALERTX_HOST": ""})()
        )
        with sqlite3.connect(path) as conn:
            _create_stream_metrics_table(conn)
        return path

    def _seed(self, db_path, loop_name, bucket_ts, total, matches, min_line_ts=None):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO stream_metrics "
                "(loop_name, bucket_ts, total_lines, match_lines, min_line_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (loop_name, bucket_ts, total, matches, min_line_ts),
            )

    def test_min_line_ts_used_as_display_ts(self, db_path, monkeypatch):
        import netalertx.log_monitor as mod

        now = int(time.time())
        bucket_ts = (now // 60) * 60 - 120
        actual_first_line = bucket_ts + 9
        self._seed(db_path, mod._LOOP_NAME, bucket_ts, 4, 1, actual_first_line)

        monkeypatch.setattr(mod, "_sparkline_bucket_total", 0)
        monkeypatch.setattr(mod, "_sparkline_bucket_matches", 0)
        monkeypatch.setattr(mod, "_sparkline_bucket_min_ts", 0)
        monkeypatch.setattr(mod, "_sparkline_current_minute", now // 60)

        result = mod.get_netalertx_sparkline_data(bucket_size="1m", time_range="1h")
        past = [b for b in result["buckets"] if b[0] == actual_first_line]
        assert past, "nax bucket with min_line_ts display_ts not found"
        assert past[0][1] == 4

    def test_null_min_line_ts_falls_back_to_bucket_ts(self, db_path, monkeypatch):
        import netalertx.log_monitor as mod

        now = int(time.time())
        bucket_ts = (now // 60) * 60 - 120
        self._seed(db_path, mod._LOOP_NAME, bucket_ts, 6, 0, None)

        monkeypatch.setattr(mod, "_sparkline_bucket_total", 0)
        monkeypatch.setattr(mod, "_sparkline_bucket_matches", 0)
        monkeypatch.setattr(mod, "_sparkline_bucket_min_ts", 0)
        monkeypatch.setattr(mod, "_sparkline_current_minute", now // 60)

        result = mod.get_netalertx_sparkline_data(bucket_size="1m", time_range="1h")
        past = [b for b in result["buckets"] if b[0] == bucket_ts]
        assert past, "nax bucket with fallback bucket_ts not found"
        assert past[0][1] == 6
