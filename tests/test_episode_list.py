"""Tests for utils/debug/episode_list.py and rotate_old_episodes (#1059)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from utils.debug import episode_list as el
from utils.debug.episode_list import (
    EpisodeRow,
    filter_rows,
    format_started,
    join_session_outcomes,
    outcome_options,
    scan_episode_dirs,
    summarize_episode,
)
from utils.debug.episode_writer import rotate_old_episodes


def _write(root: Path, folder: str, data) -> Path:
    d = root / folder
    d.mkdir(parents=True)
    (d / "episode_data.json").write_text(
        data if isinstance(data, str) else json.dumps(data)
    )
    return d


def _row(**kw: Any) -> EpisodeRow:
    base: dict[str, Any] = dict(
        episode_id="x",
        folder="t_x",
        trigger="t",
        session_type="t",
        signature="",
        outcome="success",
        started_at="",
        sort_ts=0.0,
        duration_seconds=None,
        model="",
        llm_calls=0,
        tool_calls=0,
        schema_version=2,
    )
    base.update(kw)
    return EpisodeRow(**base)


class TestSummarizeEpisode:
    def test_v2_fields(self):
        row = summarize_episode(
            Path("repair_poll_abc-1"),
            {
                "schema_version": 2,
                "trigger": "repair_poll",
                "session_type": "repair_issue",
                "signature": "repair:x",
                "outcome": "stuck",
                "started_at": "2026-11-02T10:00:00Z",
                "duration_seconds": 12.5,
                "model": "m",
                "llm_calls": [{}, {}],
                "tool_calls": [{}],
            },
        )
        assert row.episode_id == "abc-1"
        assert row.session_type == "repair_issue"
        assert row.outcome == "stuck"
        assert row.llm_calls == 2 and row.tool_calls == 1
        assert row.duration_seconds == 12.5
        assert format_started(row) == "2026-11-02 10:00 UTC"

    def test_v1_defaults(self):
        row = summarize_episode(
            Path("update_poll_def-2"),
            {"outcome": "success", "timestamp": "2026-10-20 14:35:37"},
        )
        assert row.trigger == "update_poll"  # from folder name
        assert row.session_type == "update_poll"
        assert row.schema_version == 1
        assert row.duration_seconds is None
        assert row.sort_ts > 0

    def test_unparseable_timestamp(self):
        row = summarize_episode(Path("t_e"), {"timestamp": "not a date"})
        assert row.sort_ts == 0.0
        assert row.outcome == "unknown"
        assert format_started(row) == "not a date"
        assert format_started(_row()) == "—"


class TestScanEpisodeDirs:
    def test_missing_dir(self, tmp_path):
        assert scan_episode_dirs(tmp_path / "nope") == []

    def test_sorted_newest_first_and_skips_bad(self, tmp_path):
        _write(tmp_path, "a_old", {"started_at": "2026-01-01T00:00:00Z"})
        _write(tmp_path, "a_new", {"started_at": "2026-06-01T00:00:00Z"})
        _write(tmp_path, "a_bad", "{not json")
        _write(tmp_path, "a_list", "[1, 2]")
        (tmp_path / "a_empty").mkdir()
        (tmp_path / "stray.txt").write_text("x")
        rows = scan_episode_dirs(tmp_path)
        assert [r.episode_id for r in rows] == ["new", "old"]

    def test_chat_twin_dropped(self, tmp_path):
        _write(tmp_path, "manual_uuid-1", {"trigger": "manual"})
        _write(
            tmp_path,
            "session_7",
            {"trigger": "chat", "agent_episode_id": "uuid-1"},
        )
        _write(tmp_path, "manual_uuid-2", {"trigger": "manual"})
        ids = {r.episode_id for r in scan_episode_dirs(tmp_path)}
        assert ids == {"7", "uuid-2"}

    def test_cache_reused_until_file_changes(self, tmp_path, monkeypatch):
        d = _write(tmp_path, "a_c", {"outcome": "success"})
        el._cache.clear()
        scan_episode_dirs(tmp_path)
        calls = []
        real = el.summarize_episode
        monkeypatch.setattr(
            el, "summarize_episode", lambda f, data: calls.append(1) or real(f, data)
        )
        scan_episode_dirs(tmp_path)
        assert calls == []
        (d / "episode_data.json").write_text(json.dumps({"outcome": "failed!"}))
        rows = scan_episode_dirs(tmp_path)
        assert calls == [1]
        assert rows[0].outcome == "failed!"


class TestJoinSessionOutcomes:
    def _db(self, tmp_path) -> Path:
        db = tmp_path / "s.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE session_outcomes (episode_id TEXT PRIMARY KEY,"
                " signature TEXT, verification TEXT, verified_by TEXT, card_id TEXT)"
            )
            conn.execute(
                "INSERT INTO session_outcomes VALUES"
                " ('e1', 'sig:db', 'success', 'user', 'card-1')"
            )
            conn.execute(
                "INSERT INTO session_outcomes VALUES"
                " ('uuid-c', 'chat:x', 'failure', 'system', NULL)"
            )
        return db

    def test_joins_by_episode_and_agent_id(self, tmp_path):
        db = self._db(tmp_path)
        rows = [
            _row(episode_id="e1"),
            _row(episode_id="7", agent_episode_id="uuid-c", signature="keep"),
            _row(episode_id="none"),
        ]
        out = join_session_outcomes(rows, db)
        assert (out[0].verification, out[0].verified_by, out[0].card_id) == (
            "success",
            "user",
            "card-1",
        )
        assert out[0].signature == "sig:db"  # filled when missing
        assert out[1].verification == "failure"
        assert out[1].signature == "keep"  # file signature wins
        assert out[2].verification == ""

    def test_missing_db_or_rows(self, tmp_path):
        rows = [_row()]
        assert join_session_outcomes(rows, tmp_path / "nope.db") == rows
        assert join_session_outcomes([], tmp_path / "nope.db") == []

    def test_missing_table_leaves_rows(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(db).close()
        rows = [_row()]
        assert join_session_outcomes(rows, db) == rows

    def test_open_failure_leaves_rows(self, tmp_path, monkeypatch):
        db = tmp_path / "x.db"
        db.write_text("")

        def _boom(*a, **k):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(el.sqlite3, "connect", _boom)
        rows = [_row()]
        assert join_session_outcomes(rows, db) == rows


class TestFilterRows:
    def test_filters(self):
        rows = [
            _row(episode_id="a", outcome="success", session_type="repair"),
            _row(episode_id="b", outcome="stuck", session_type="repair"),
            _row(episode_id="c", outcome="failed", session_type="chat"),
        ]
        assert [r.episode_id for r in filter_rows(rows)] == ["a", "b", "c"]
        assert [r.episode_id for r in filter_rows(rows, "stuck")] == ["b"]
        assert [r.episode_id for r in filter_rows(rows, "non_success")] == ["b", "c"]
        assert [r.episode_id for r in filter_rows(rows, "", "chat")] == ["c"]
        assert filter_rows(rows, "success", "chat") == []

    def test_outcome_options(self):
        opts = outcome_options([_row(outcome="weird"), _row(outcome="success")])
        assert opts[: len(el.STANDARD_OUTCOMES)] == list(el.STANDARD_OUTCOMES)
        assert opts[-1] == "weird"


class TestRotateOldEpisodes:
    def test_removes_only_old_dirs(self, tmp_path):
        old = tmp_path / "a_old"
        old.mkdir()
        (old / "episode_data.json").write_text("{}")
        new = tmp_path / "a_new"
        new.mkdir()
        stray = tmp_path / "file.txt"
        stray.write_text("x")
        past = time.time() - 10 * 86400
        os.utime(old, (past, past))
        os.utime(stray, (past, past))
        assert rotate_old_episodes(tmp_path, 7) == 1
        assert not old.exists()
        assert new.exists() and stray.exists()

    def test_missing_dir(self, tmp_path):
        assert rotate_old_episodes(tmp_path / "nope", 7) == 0
