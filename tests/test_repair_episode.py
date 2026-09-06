"""Tests for utils/repair_episode.py helpers."""

import sqlite3
from typing import Any

import pytest

from utils.repair.repair_episode import (
    RepairEpisode,
    format_episode_for_embedding,
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
