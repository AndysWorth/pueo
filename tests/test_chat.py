"""Tests for conversational agent — item 72 (partial: item 66 scope).

Covers: TestRememberRecall (remember/recall tool methods on ToolExecutor).
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

import ha_agent_advanced
from utils.autonomy import FakeAutonomyGate
from utils.notify import FakeNotifier
from utils.ssh_client import FakeSSHClient
from utils.tool_executor import ToolExecutor
from utils.tool_registry import ToolCall


@pytest.fixture
def db_path(monkeypatch, tmp_path):
    path = str(tmp_path / "test_agent.db")
    monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
    ha_agent_advanced.init_local_database()
    return path


@pytest.fixture
def executor(db_path):
    return ToolExecutor(
        ha_ssh_client=FakeSSHClient(),
        gate=FakeAutonomyGate(),
        notifier=FakeNotifier(),
        db_path=db_path,
    )


class TestRememberRecall:
    def test_remember_stores_row(self, executor, db_path):
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    name="remember",
                    arguments={"key": "server", "content": "NAS is at 192.168.1.100"},
                )
            )
        )
        assert result.success
        assert "server" in result.output

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT key, content FROM agent_memory").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "server"
        assert "192.168.1.100" in rows[0][1]

    def test_recall_finds_by_keyword(self, executor, db_path):
        asyncio.run(
            executor.execute(
                ToolCall(
                    name="remember",
                    arguments={
                        "key": "media_server",
                        "content": "media server IP is 192.168.1.50",
                    },
                )
            )
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="recall", arguments={"query": "media server"})
            )
        )
        assert result.success
        assert "192.168.1.50" in result.output

    def test_recall_empty_on_no_match(self, executor):
        result = asyncio.run(
            executor.execute(
                ToolCall(name="recall", arguments={"query": "nonexistent_xyz_123"})
            )
        )
        assert result.success
        assert "Nothing found" in result.output

    def test_recall_respects_top_k(self, db_path, monkeypatch):
        import utils.tool_executor as te_mod

        monkeypatch.setattr(te_mod, "CHAT_MEMORY_TOP_K", 3)
        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
            db_path=db_path,
        )

        for i in range(5):
            asyncio.run(
                ex.execute(
                    ToolCall(
                        name="remember",
                        arguments={
                            "key": f"item{i}",
                            "content": f"value {i} matching keyword",
                        },
                    )
                )
            )

        result = asyncio.run(
            ex.execute(ToolCall(name="recall", arguments={"query": "matching keyword"}))
        )
        assert result.success
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 3

    def test_remember_uses_agent_source_by_default(self, db_path, executor):
        asyncio.run(
            executor.execute(
                ToolCall(
                    name="remember",
                    arguments={"key": "note", "content": "something important"},
                )
            )
        )
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT source FROM agent_memory").fetchall()
        assert rows[0][0] == "agent"

    def test_recall_matches_on_key(self, executor):
        asyncio.run(
            executor.execute(
                ToolCall(
                    name="remember",
                    arguments={
                        "key": "unique_key_xyz",
                        "content": "unrelated content here",
                    },
                )
            )
        )
        result = asyncio.run(
            executor.execute(
                ToolCall(name="recall", arguments={"query": "unique_key_xyz"})
            )
        )
        assert result.success
        assert "unique_key_xyz" in result.output
