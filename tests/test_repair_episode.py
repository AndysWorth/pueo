"""Tests for embedding helpers added to utils/repair_episode.py."""

import asyncio
import sqlite3
import time
from typing import Any

import pytest

from utils.repair_episode import (
    RepairEpisode,
    format_episode_for_embedding,
    get_unembedded_successful_episodes,
    mark_episode_embedded,
)
from utils.agent.tool_registry import ToolCall


def _make_episode(**kwargs: Any) -> RepairEpisode:
    defaults: dict[str, Any] = dict(
        trigger="ha_log",
        symptoms=["recorder DB growing"],
        tool_sequence=[ToolCall(name="read_logs", arguments={})],
        hypothesis_chain=["high-frequency polling", "recorder not purging"],
        fix_applied="reduced recorder retention to 30 days",
        verification_result=True,
        model_used="qwen2.5-coder:7b",
        escalated=False,
        duration_seconds=42.5,
    )
    defaults.update(kwargs)
    return RepairEpisode(**defaults)


def _init_db(path: str) -> None:
    """Minimal schema for repair_episodes with embedded_at column."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS repair_episodes (
                id TEXT PRIMARY KEY,
                timestamp REAL,
                trigger TEXT,
                symptoms TEXT,
                tool_sequence TEXT,
                hypothesis_chain TEXT,
                fix_applied TEXT,
                verification_result INTEGER,
                model_used TEXT,
                escalated INTEGER,
                duration_seconds REAL,
                submitted_at REAL,
                pr_url TEXT,
                embedded_at REAL DEFAULT NULL
            )
            """
        )
        conn.commit()


class TestFormatEpisodeForEmbedding:
    def test_includes_trigger(self):
        ep = _make_episode(trigger="ha_log")
        text = format_episode_for_embedding(ep)
        assert "ha_log" in text

    def test_includes_symptoms(self):
        ep = _make_episode(symptoms=["recorder DB growing", "disk warn"])
        text = format_episode_for_embedding(ep)
        assert "recorder DB growing" in text
        assert "disk warn" in text

    def test_includes_hypothesis_chain(self):
        ep = _make_episode(hypothesis_chain=["high-frequency polling"])
        text = format_episode_for_embedding(ep)
        assert "high-frequency polling" in text

    def test_includes_fix_applied(self):
        ep = _make_episode(fix_applied="reduced recorder retention to 30 days")
        text = format_episode_for_embedding(ep)
        assert "reduced recorder retention" in text

    def test_no_fix_applied_omits_that_line(self):
        ep = _make_episode(fix_applied=None)
        text = format_episode_for_embedding(ep)
        assert "fix applied" not in text

    def test_verification_success_included(self):
        ep = _make_episode(verification_result=True)
        text = format_episode_for_embedding(ep)
        assert "success" in text

    def test_verification_failed_included(self):
        ep = _make_episode(verification_result=False)
        text = format_episode_for_embedding(ep)
        assert "failed" in text


class TestGetUnembeddedSuccessfulEpisodes:
    def test_returns_successful_unembedded(self, tmp_path):
        db = str(tmp_path / "test.db")
        _init_db(db)
        ep = _make_episode(verification_result=True)
        from utils.repair_episode import serialize_episode

        serialize_episode(db, ep)
        results = get_unembedded_successful_episodes(db)
        assert len(results) == 1
        assert results[0].id == ep.id

    def test_excludes_failed_verification(self, tmp_path):
        db = str(tmp_path / "test.db")
        _init_db(db)
        ep = _make_episode(verification_result=False)
        from utils.repair_episode import serialize_episode

        serialize_episode(db, ep)
        results = get_unembedded_successful_episodes(db)
        assert results == []

    def test_excludes_already_embedded(self, tmp_path):
        db = str(tmp_path / "test.db")
        _init_db(db)
        ep = _make_episode(verification_result=True)
        from utils.repair_episode import serialize_episode

        serialize_episode(db, ep)
        mark_episode_embedded(db, ep.id)
        results = get_unembedded_successful_episodes(db)
        assert results == []

    def test_returns_multiple_unembedded(self, tmp_path):
        db = str(tmp_path / "test.db")
        _init_db(db)
        from utils.repair_episode import serialize_episode

        ep1 = _make_episode(verification_result=True)
        ep2 = _make_episode(verification_result=True)
        serialize_episode(db, ep1)
        serialize_episode(db, ep2)
        results = get_unembedded_successful_episodes(db)
        assert len(results) == 2


class TestMarkEpisodeEmbedded:
    def test_sets_embedded_at_timestamp(self, tmp_path):
        db = str(tmp_path / "test.db")
        _init_db(db)
        ep = _make_episode(verification_result=True)
        from utils.repair_episode import serialize_episode

        serialize_episode(db, ep)
        before = time.time()
        mark_episode_embedded(db, ep.id)
        after = time.time()

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT embedded_at FROM repair_episodes WHERE id = ?", (ep.id,)
            ).fetchone()
        assert row is not None
        assert before <= row[0] <= after

    def test_episode_no_longer_returned_as_unembedded(self, tmp_path):
        db = str(tmp_path / "test.db")
        _init_db(db)
        ep = _make_episode(verification_result=True)
        from utils.repair_episode import serialize_episode

        serialize_episode(db, ep)
        mark_episode_embedded(db, ep.id)
        assert get_unembedded_successful_episodes(db) == []


class TestEmbedLocalEpisode:
    def test_upserts_to_community_cases_and_marks_embedded(self, tmp_path):
        db = str(tmp_path / "test.db")
        _init_db(db)
        ep = _make_episode(verification_result=True)
        from utils.repair_episode import serialize_episode

        serialize_episode(db, ep)

        from utils.knowledge_store import FakeKnowledgeStore, embed_local_episode

        store = FakeKnowledgeStore()
        result = asyncio.run(embed_local_episode(ep, store, db))

        assert result is True
        # Episode should now be marked embedded
        assert get_unembedded_successful_episodes(db) == []
        # Document should be in community_cases
        chunks = store.query(
            query_text="recorder", top_k=5, collections=["community_cases"]
        )
        assert any(c.metadata.get("episode_id") == ep.id for c in chunks)

    def test_returns_false_on_store_error(self, tmp_path):
        db = str(tmp_path / "test.db")
        _init_db(db)
        ep = _make_episode(verification_result=True)

        class _FailStore:
            def upsert(self, **_):
                raise RuntimeError("store broken")

        from utils.knowledge_store import embed_local_episode

        result = asyncio.run(embed_local_episode(ep, _FailStore(), db))
        assert result is False
