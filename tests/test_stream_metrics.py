"""Tests for stream_metrics SQLite persistence and sparkline endpoints (issue #513)."""

import importlib
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Migration v26 ─────────────────────────────────────────────────────────────


def _reload_and_init_db(pueo_dirs):
    """Reload config + ha_agent_advanced so DB_PATH points to tmp_path, then init."""
    import importlib as _imp
    import sys

    _imp.reload(sys.modules["config"])
    import config as cfg
    import agents.ha_agent_advanced as adv

    _imp.reload(adv)
    adv.init_local_database()
    return cfg


class TestMigrationV26:
    def test_creates_stream_metrics_table(self, pueo_dirs, isolated_config):
        cfg = _reload_and_init_db(pueo_dirs)

        with sqlite3.connect(cfg.DB_PATH) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "stream_metrics" in tables

    def test_creates_stream_metrics_index(self, pueo_dirs, isolated_config):
        cfg = _reload_and_init_db(pueo_dirs)

        with sqlite3.connect(cfg.DB_PATH) as conn:
            indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        assert "idx_stream_metrics_ts" in indexes

    def test_sandbox_engine_migration_also_creates_table(
        self, pueo_dirs, isolated_config
    ):
        import importlib as _imp

        _imp.reload(sys.modules["config"])
        import config as cfg
        import agents.ha_agent_sandbox_engine as sb

        _imp.reload(sb)
        sb.init_local_database()

        with sqlite3.connect(cfg.DB_PATH) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "stream_metrics" in tables


# ── _persist_sparkline_bucket ─────────────────────────────────────────────────


class TestPersistSparklineBucket:
    def test_writes_row_to_db(self, pueo_dirs, isolated_config):
        import importlib as _imp

        cfg = _reload_and_init_db(pueo_dirs)
        import agents.ha_log_monitor as mon

        _imp.reload(mon)

        bucket_ts = int(time.time() // 60) * 60
        mon._persist_sparkline_bucket("ha_log_monitor", bucket_ts, 42, 3, 0)

        with sqlite3.connect(cfg.DB_PATH) as conn:
            row = conn.execute(
                "SELECT total_lines, match_lines FROM stream_metrics "
                "WHERE loop_name = 'ha_log_monitor' AND bucket_ts = ?",
                (bucket_ts,),
            ).fetchone()
        assert row == (42, 3)

    def test_netalertx_writes_row(self, pueo_dirs, isolated_config):
        import importlib as _imp

        cfg = _reload_and_init_db(pueo_dirs)
        import netalertx.log_monitor as nax_mon

        _imp.reload(nax_mon)

        bucket_ts = int(time.time() // 60) * 60 - 60
        nax_mon._persist_sparkline_bucket(bucket_ts, 10, 1, 0)

        with sqlite3.connect(cfg.DB_PATH) as conn:
            row = conn.execute(
                "SELECT total_lines, match_lines FROM stream_metrics "
                "WHERE loop_name = 'netalertx' AND bucket_ts = ?",
                (bucket_ts,),
            ).fetchone()
        assert row == (10, 1)


# ── _init_sparkline_db ────────────────────────────────────────────────────────


class TestInitSparklineDb:
    def test_prepopulates_deque_from_db(self, pueo_dirs, isolated_config):
        import importlib as _imp

        cfg = _reload_and_init_db(pueo_dirs)
        import agents.ha_log_monitor as mon

        _imp.reload(mon)

        base_ts = (int(time.time()) // 60) * 60 - 300
        with sqlite3.connect(cfg.DB_PATH) as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO stream_metrics (loop_name, bucket_ts, total_lines, match_lines) "
                    "VALUES (?, ?, ?, ?)",
                    ("ha_log_monitor", base_ts + i * 60, 10 + i, i),
                )

        mon._core_state.buckets.clear()
        mon._init_sparkline_db(mon._core_state)

        assert len(mon._core_state.buckets) == 5
        ts_vals = [b[0] for b in mon._core_state.buckets]
        assert ts_vals == sorted(ts_vals)

    def test_graceful_if_table_missing(self, pueo_dirs, isolated_config):
        import importlib as _imp

        _imp.reload(sys.modules["config"])
        import agents.ha_log_monitor as mon

        _imp.reload(mon)
        # DB has no tables at all — should not raise
        mon._init_sparkline_db(mon._core_state)


# ── get_ha_log_sparkline_data ─────────────────────────────────────────────────


class TestGetHaLogSparklineData:
    def test_returns_new_format(self, pueo_dirs, isolated_config):
        import importlib as _imp

        cfg = _reload_and_init_db(pueo_dirs)
        import agents.ha_log_monitor as mon

        _imp.reload(mon)

        now_min = (int(time.time()) // 60) * 60
        with sqlite3.connect(cfg.DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO stream_metrics "
                "(loop_name, bucket_ts, total_lines, match_lines) VALUES (?, ?, ?, ?)",
                ("ha_log_monitor", now_min - 60, 5, 1),
            )

        data = mon.get_ha_log_sparkline_data(bucket_size="1m", time_range="1h")
        assert "buckets" in data
        assert data["bucket_size"] == "1m"
        assert data["time_range"] == "1h"
        buckets = data["buckets"]
        assert isinstance(buckets, list)
        if buckets:
            assert len(buckets[0]) == 3

    def test_bucket_size_1h_aggregates(self, pueo_dirs, isolated_config):
        import importlib as _imp

        cfg = _reload_and_init_db(pueo_dirs)
        import agents.ha_log_monitor as mon

        _imp.reload(mon)
        mon._core_state.bucket_total = 0
        mon._core_state.bucket_matches = 0

        hour_ts = (int(time.time()) // 3600) * 3600 - 3600
        with sqlite3.connect(cfg.DB_PATH) as conn:
            for i in range(6):
                conn.execute(
                    "INSERT OR REPLACE INTO stream_metrics "
                    "(loop_name, bucket_ts, total_lines, match_lines) VALUES (?, ?, ?, ?)",
                    ("ha_log_monitor", hour_ts + i * 60, 10, 1),
                )

        data = mon.get_ha_log_sparkline_data(bucket_size="1h", time_range="24h")
        agg_totals = {b[0]: b[1] for b in data["buckets"]}
        assert agg_totals.get(hour_ts, 0) == 60  # 6 minutes × 10 lines each

    def test_pan_reaches_data_older_than_time_range(self, pueo_dirs, isolated_config):
        """All historical data is returned so pan buttons can reach it."""
        import importlib as _imp

        cfg = _reload_and_init_db(pueo_dirs)
        import agents.ha_log_monitor as mon

        _imp.reload(mon)
        mon._core_state.bucket_total = 0
        mon._core_state.bucket_matches = 0

        old_ts = int(time.time()) - 8 * 24 * 3600  # 8 days ago — beyond time_range=7d
        with sqlite3.connect(cfg.DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO stream_metrics "
                "(loop_name, bucket_ts, total_lines, match_lines) VALUES (?, ?, ?, ?)",
                ("ha_log_monitor", old_ts, 99, 0),
            )

        data = mon.get_ha_log_sparkline_data(bucket_size="1d", time_range="7d")
        ts_vals = [b[0] for b in data["buckets"]]
        # Old row must be present so the pan button can reach it
        assert any(abs(t - old_ts) < 86400 for t in ts_vals)

    def test_empty_db_returns_slots_for_time_range(self, pueo_dirs, isolated_config):
        """With no DB rows, returned buckets still cover at least time_range slots."""
        import importlib as _imp

        _reload_and_init_db(pueo_dirs)
        import agents.ha_log_monitor as mon

        _imp.reload(mon)
        mon._core_state.bucket_total = 0
        mon._core_state.bucket_matches = 0
        mon._core_state.current_minute = 0

        data = mon.get_ha_log_sparkline_data(bucket_size="1h", time_range="6h")
        # 6h / 1h = 6 slots minimum
        assert len(data["buckets"]) >= 6
