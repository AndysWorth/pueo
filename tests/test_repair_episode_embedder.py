"""Tests for utils/knowledge/repair_episode_embedder.py."""

import json
import sqlite3
from typing import Any
from unittest.mock import patch

import pytest

from utils.agent.tool_registry import ToolCall
from utils.knowledge.knowledge_store import FakeKnowledgeStore
from utils.knowledge.repair_episode_embedder import (
    _mark_episodes_embedded,
    embed_repair_episodes,
)
from utils.repair.repair_episode import RepairEpisode, serialize_episode


def _make_episode(**kwargs: Any) -> RepairEpisode:
    defaults: dict[str, Any] = dict(
        trigger="ha_log",
        symptoms=["recorder DB growing"],
        tool_sequence=[ToolCall(name="read_logs", arguments={})],
        hypothesis_chain=["high-frequency polling"],
        fix_applied="reduced recorder retention",
        verification_result=True,
        model_used="qwen2.5:7b",
        escalated=False,
        duration_seconds=30.0,
    )
    defaults.update(kwargs)
    return RepairEpisode(**defaults)


def _create_db(path: str) -> None:
    """Minimal repair_episodes schema for tests."""
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
                submitted_at REAL DEFAULT NULL,
                pr_url TEXT DEFAULT NULL,
                embedded_at REAL DEFAULT NULL,
                initial_context TEXT DEFAULT NULL,
                activity_type TEXT DEFAULT NULL
            )
            """
        )
        conn.commit()


def _insert_episode(db_path: str, episode: RepairEpisode) -> None:
    """Insert an episode directly without setting embedded_at."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO repair_episodes
                (id, timestamp, trigger, symptoms, tool_sequence, hypothesis_chain,
                 fix_applied, verification_result, model_used, escalated,
                 duration_seconds, submitted_at, pr_url, embedded_at,
                 initial_context, activity_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
            """,
            (
                episode.id,
                episode.timestamp,
                episode.trigger,
                json.dumps(episode.symptoms),
                json.dumps(
                    [
                        {"name": tc.name, "arguments": tc.arguments}
                        for tc in episode.tool_sequence
                    ]
                ),
                json.dumps(episode.hypothesis_chain),
                episode.fix_applied,
                int(episode.verification_result),
                episode.model_used,
                int(episode.escalated),
                episode.duration_seconds,
            ),
        )
        conn.commit()


class TestEmbedRepairEpisodes:
    def test_upserts_unembedded_episodes(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep = _make_episode()
        _insert_episode(db, ep)

        store = FakeKnowledgeStore()
        count = embed_repair_episodes(store, db)

        assert count == 1
        assert store.collection_count("repair_history") == 1

    def test_marks_embedded_at(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep = _make_episode()
        _insert_episode(db, ep)

        store = FakeKnowledgeStore()
        embed_repair_episodes(store, db)

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT embedded_at FROM repair_episodes WHERE id = ?", (ep.id,)
            ).fetchone()
        assert row is not None
        assert row[0] is not None

    def test_skips_already_embedded(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep = _make_episode()
        _insert_episode(db, ep)
        # Mark as already embedded
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE repair_episodes SET embedded_at = 12345.0 WHERE id = ?",
                (ep.id,),
            )

        store = FakeKnowledgeStore()
        count = embed_repair_episodes(store, db)

        assert count == 0
        assert store.collection_count("repair_history") == 0

    def test_returns_zero_when_db_empty(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)

        store = FakeKnowledgeStore()
        count = embed_repair_episodes(store, db)

        assert count == 0

    def test_returns_zero_on_db_error(self, tmp_path):
        store = FakeKnowledgeStore()
        # Non-existent DB
        count = embed_repair_episodes(store, str(tmp_path / "missing.db"))
        assert count == 0

    def test_embeds_multiple_episodes(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep1 = _make_episode()
        ep2 = _make_episode(trigger="manual", verification_result=False)
        _insert_episode(db, ep1)
        _insert_episode(db, ep2)

        store = FakeKnowledgeStore()
        count = embed_repair_episodes(store, db)

        assert count == 2
        assert store.collection_count("repair_history") == 2

    def test_metadata_includes_outcome_and_trigger(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep = _make_episode(trigger="ha_log", verification_result=True)
        _insert_episode(db, ep)

        store = FakeKnowledgeStore()
        embed_repair_episodes(store, db)

        chunks = store.query("recorder", top_k=5, collections=["repair_history"])
        assert len(chunks) == 1
        assert chunks[0].metadata["outcome"] == "success"
        assert chunks[0].metadata["trigger"] == "ha_log"

    def test_failed_episode_outcome_is_failed(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep = _make_episode(verification_result=False)
        _insert_episode(db, ep)

        store = FakeKnowledgeStore()
        embed_repair_episodes(store, db)

        chunks = store.query("recorder", top_k=5, collections=["repair_history"])
        assert chunks[0].metadata["outcome"] == "failed"

    def test_upsert_failure_returns_zero(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep = _make_episode()
        _insert_episode(db, ep)

        class _BrokenStore(FakeKnowledgeStore):
            def upsert(self, *a, **kw):
                raise RuntimeError("store broken")

        count = embed_repair_episodes(_BrokenStore(), db)
        assert count == 0

    def test_embedded_at_not_set_when_upsert_fails(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep = _make_episode()
        _insert_episode(db, ep)

        class _BrokenStore(FakeKnowledgeStore):
            def upsert(self, *a, **kw):
                raise RuntimeError("store broken")

        embed_repair_episodes(_BrokenStore(), db)

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT embedded_at FROM repair_episodes WHERE id = ?", (ep.id,)
            ).fetchone()
        assert row[0] is None


class TestMarkEpisodesEmbedded:
    def test_sets_embedded_at(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        ep = _make_episode()
        _insert_episode(db, ep)

        _mark_episodes_embedded(db, [ep.id], 9999.0)

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT embedded_at FROM repair_episodes WHERE id = ?", (ep.id,)
            ).fetchone()
        assert row[0] == 9999.0

    def test_empty_list_is_noop(self, tmp_path):
        db = str(tmp_path / "state.db")
        _create_db(db)
        # Should not raise
        _mark_episodes_embedded(db, [], 1.0)


class TestRepairHistoryInCollections:
    def test_repair_history_in_collections(self):
        from utils.knowledge.knowledge_store import COLLECTIONS

        assert "repair_history" in COLLECTIONS

    def test_fake_store_accepts_repair_history_upsert(self):
        store = FakeKnowledgeStore()
        store.upsert(
            "repair_history",
            ["ep_1"],
            ["trigger: ha_log\nfix: replaced yaml"],
            [{"outcome": "success", "trigger": "ha_log"}],
        )
        assert store.collection_count("repair_history") == 1

    def test_query_includes_repair_history_by_default(self):
        store = FakeKnowledgeStore()
        store.upsert(
            "repair_history",
            ["ep_1"],
            ["recorder db growing caused by high retention"],
            [{"outcome": "success", "trigger": "ha_log"}],
        )
        chunks = store.query("recorder db growing", top_k=5)
        collections = {c.collection for c in chunks}
        assert "repair_history" in collections
