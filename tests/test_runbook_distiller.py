"""Tests for utils/knowledge/runbook_distiller.py."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from pydantic import ValidationError

from utils.knowledge.runbook_distiller import (
    DistilledRunbook,
    _count_evidence_calls,
    _get_existing_by_signature,
    distill_and_persist,
    qualifies_for_distillation,
    resolve_gap,
    write_gap,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step(name: str, step_number: int = 1):
    from utils.agent.tool_registry import AgentStep, ToolCall, ToolResult

    return AgentStep(
        step_number=step_number,
        tool_call=ToolCall(name=name, arguments={}),
        tool_result=ToolResult(tool_name=name, success=True, output="ok"),
        timestamp=0.0,
    )


def _make_steps(*names: str):
    return [_make_step(n, i + 1) for i, n in enumerate(names)]


def _make_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE agent_strategies ("
            "id TEXT PRIMARY KEY, "
            "title TEXT NOT NULL, "
            "trigger_pattern TEXT NOT NULL, "
            "approach TEXT NOT NULL, "
            "runbook_state TEXT NOT NULL DEFAULT 'candidate', "
            "created_at TEXT NOT NULL, "
            "signature TEXT, "
            "version INTEGER DEFAULT 1, "
            "episode_refs TEXT, "
            "resolved_at TEXT, "
            "resolved_by TEXT"
            ")"
        )
        conn.commit()
    return db_path


# ---------------------------------------------------------------------------
# DistilledRunbook schema tests
# ---------------------------------------------------------------------------


class TestDistilledRunbookSchema:
    def test_valid_construction(self):
        rb = DistilledRunbook(
            title="Fix ZHA coordinator crash",
            when_to_use="When ZHA integration becomes unavailable after a USB event",
            recommended_tools=["read_logs", "run_ha_command", "read_config"],
            hypotheses=["USB coordinator disconnected", "HA lost USB path"],
            fix_summary="Restarted ZHA integration via HA CLI",
            pitfalls=["Do not unplug USB while HA is running"],
        )
        assert rb.title == "Fix ZHA coordinator crash"
        assert len(rb.recommended_tools) == 3
        assert len(rb.pitfalls) == 1

    def test_pitfalls_default_empty(self):
        rb = DistilledRunbook(
            title="T",
            when_to_use="When X",
            recommended_tools=["read_logs"],
            hypotheses=["H1"],
            fix_summary="Fixed it",
        )
        assert rb.pitfalls == []

    def test_invalid_missing_required_fields(self):
        with pytest.raises(ValidationError):
            DistilledRunbook(
                when_to_use="When X",
                recommended_tools=["read_logs"],
                hypotheses=[],
                fix_summary="ok",
            )  # missing title

    def test_json_round_trip(self):
        rb = DistilledRunbook(
            title="Diagnose update failure",
            when_to_use="When a core update check returns an error",
            recommended_tools=[
                "get_update_release_notes",
                "check_config_against_breaking_change",
            ],
            hypotheses=["Breaking change in recorder config"],
            fix_summary="Renamed keep_days in recorder config",
            pitfalls=["apply_filter=True causes a 400 error"],
        )
        as_json = rb.model_dump_json()
        restored = DistilledRunbook.model_validate_json(as_json)
        assert restored.title == rb.title
        assert restored.pitfalls == rb.pitfalls


# ---------------------------------------------------------------------------
# Qualifying rule
# ---------------------------------------------------------------------------


class TestQualifiesForDistillation:
    def test_success_with_enough_evidence_qualifies(self):
        steps = _make_steps("read_config", "read_logs", "run_ha_command")
        assert qualifies_for_distillation("success", steps)

    def test_success_with_fewer_than_3_evidence_calls_does_not_qualify(self):
        steps = _make_steps("read_config", "read_logs", "query_knowledge")
        # query_knowledge is not an evidence tool
        assert not qualifies_for_distillation("success", steps)

    def test_non_success_does_not_qualify(self):
        steps = _make_steps("read_config", "read_logs", "run_ha_command")
        assert not qualifies_for_distillation("exhausted", steps)

    def test_failed_does_not_qualify(self):
        steps = _make_steps("read_config", "read_logs", "run_ha_command", "fetch_url")
        assert not qualifies_for_distillation("failed", steps)

    def test_empty_steps_do_not_qualify(self):
        assert not qualifies_for_distillation("success", [])

    def test_count_evidence_tools_correct(self):
        # Terminal tools, query_knowledge, and save_runbook are not evidence tools
        steps = _make_steps(
            "read_config",  # evidence
            "query_knowledge",  # not evidence
            "finish_repair",  # not evidence
            "read_logs",  # evidence
            "run_ha_command",  # evidence
        )
        assert _count_evidence_calls(steps) == 3


# ---------------------------------------------------------------------------
# write_gap
# ---------------------------------------------------------------------------


class TestWriteGap:
    def test_writes_sqlite_row(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        steps = _make_steps("read_config", "query_knowledge")
        write_gap(
            db_path,
            store,
            "update:core:unknown",
            "update_poll",
            steps,
            "exhausted",
            "ep1",
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT runbook_state, signature FROM agent_strategies"
            ).fetchone()
        assert row[0] == "gap"
        assert row[1] == "update:core:unknown"

    def test_writes_chroma_to_knowledge_gaps(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        write_gap(db_path, store, "sig:foo", "repair", [], "failed", "")

        chunks = store.query("sig:foo", top_k=5, collections=["knowledge_gaps"])
        assert len(chunks) == 1
        assert "sig:foo" in chunks[0].text

    def test_records_episode_ref(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        write_gap(db_path, FakeKnowledgeStore(), "s:x", "t", [], "failed", "myepisode")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT episode_refs FROM agent_strategies").fetchone()
        assert "myepisode" in json.loads(row[0])


# ---------------------------------------------------------------------------
# resolve_gap
# ---------------------------------------------------------------------------


class TestResolveGap:
    def test_marks_gap_resolved(self, tmp_path):
        db_path = _make_db(tmp_path)
        # Insert a gap row
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO agent_strategies"
                " (id, title, trigger_pattern, approach, runbook_state, created_at, signature)"
                " VALUES ('g1', 'Gap', 'trigger', 'approach', 'gap', datetime('now'), 'sig:foo')"
            )
            conn.commit()

        resolve_gap(db_path, "sig:foo", "ep99")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT runbook_state, resolved_by FROM agent_strategies WHERE id='g1'"
            ).fetchone()
        assert row[0] == "resolved"
        assert row[1] == "ep99"

    def test_does_not_touch_non_gap_rows(self, tmp_path):
        db_path = _make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO agent_strategies"
                " (id, title, trigger_pattern, approach, runbook_state, created_at, signature)"
                " VALUES ('c1', 'Cand', 'trigger', 'approach', 'candidate', datetime('now'), 'sig:foo')"
            )
            conn.commit()

        resolve_gap(db_path, "sig:foo", "ep99")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT runbook_state FROM agent_strategies WHERE id='c1'"
            ).fetchone()
        assert row[0] == "candidate"


# ---------------------------------------------------------------------------
# distill_and_persist
# ---------------------------------------------------------------------------


class _FakeLLMWithRunbook:
    """LLM stub that always returns a valid DistilledRunbook JSON."""

    def __init__(self):
        self._runbook = DistilledRunbook(
            title="Diagnosing core update risks",
            when_to_use="When a core update has release notes with breaking changes",
            recommended_tools=[
                "get_update_release_notes",
                "check_config_against_breaking_change",
            ],
            hypotheses=[
                "Breaking change in recorder config",
                "Template syntax deprecated",
            ],
            fix_summary="Config updated to new recorder syntax",
            pitfalls=["apply_filter=True causes a 400 error"],
        )

    async def chat(self, model, messages, options, format):  # noqa: A002
        return {"message": {"content": self._runbook.model_dump_json()}}

    async def chat_with_tools(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


class TestDistillAndPersist:
    def test_non_success_writes_gap_not_candidate(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        steps = _make_steps("read_config", "read_logs", "run_ha_command")
        asyncio.run(
            distill_and_persist(
                trigger="update_poll",
                signature="update:core:ha",
                steps=steps,
                outcome="exhausted",
                episode_id="ep1",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
            )
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT runbook_state FROM agent_strategies").fetchone()
        assert row is not None
        assert row[0] == "gap"
        # Nothing in strategies
        assert store.collection_count("strategies") == 0

    def test_thin_success_does_not_write(self, tmp_path, monkeypatch):
        """Success with < 3 evidence calls → no gap, no candidate."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        steps = _make_steps("query_knowledge", "finish_repair")
        asyncio.run(
            distill_and_persist(
                trigger="repair",
                signature="repair:zha:device_offline",
                steps=steps,
                outcome="success",
                episode_id="ep2",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
            )
        )

        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0

    def test_success_with_evidence_inserts_candidate(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        monkeypatch.setattr(
            "utils.knowledge.runbook_distiller.make_llm_client",
            lambda: _FakeLLMWithRunbook(),
        )
        steps = _make_steps(
            "get_update_release_notes",
            "check_config_against_breaking_change",
            "read_config",
        )
        asyncio.run(
            distill_and_persist(
                trigger="update_poll",
                signature="update:core:ha",
                steps=steps,
                outcome="success",
                episode_id="ep3",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
            )
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT runbook_state, signature, version FROM agent_strategies"
            ).fetchone()
        assert row is not None
        assert row[0] == "candidate"
        assert row[1] == "update:core:ha"
        assert row[2] == 1

        chunks = store.query("update", top_k=5, collections=["strategies"])
        assert len(chunks) == 1

    def test_second_success_merges_candidate(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        monkeypatch.setattr(
            "utils.knowledge.runbook_distiller.make_llm_client",
            lambda: _FakeLLMWithRunbook(),
        )
        steps = _make_steps(
            "get_update_release_notes",
            "check_config_against_breaking_change",
            "read_config",
        )

        # First distillation → insert
        asyncio.run(
            distill_and_persist(
                trigger="update_poll",
                signature="update:core:ha",
                steps=steps,
                outcome="success",
                episode_id="ep4",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
            )
        )
        # Second distillation → merge (bumps version)
        asyncio.run(
            distill_and_persist(
                trigger="update_poll",
                signature="update:core:ha",
                steps=steps,
                outcome="success",
                episode_id="ep5",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
            )
        )

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT version, episode_refs FROM agent_strategies"
            ).fetchall()
        assert len(rows) == 1  # still one row, not two
        assert rows[0][0] == 2  # version bumped
        refs = json.loads(rows[0][1])
        assert "ep4" in refs
        assert "ep5" in refs

    def test_success_resolves_existing_gap(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        monkeypatch.setattr(
            "utils.knowledge.runbook_distiller.make_llm_client",
            lambda: _FakeLLMWithRunbook(),
        )
        # Pre-insert a gap for the same signature
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO agent_strategies"
                " (id, title, trigger_pattern, approach, runbook_state, created_at, signature)"
                " VALUES ('gap1', 'Gap', 'update_poll', 'tried stuff', 'gap', datetime('now'), 'update:core:ha')"
            )
            conn.commit()

        steps = _make_steps(
            "get_update_release_notes",
            "check_config_against_breaking_change",
            "read_config",
        )
        asyncio.run(
            distill_and_persist(
                trigger="update_poll",
                signature="update:core:ha",
                steps=steps,
                outcome="success",
                episode_id="ep6",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
            )
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT runbook_state FROM agent_strategies WHERE id='gap1'"
            ).fetchone()
        assert row[0] == "resolved"

    def test_llm_failure_does_not_crash(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)

        class _BrokenLLM:
            async def chat(self, **kw):  # pragma: no cover
                raise RuntimeError("Ollama down")

        monkeypatch.setattr(
            "utils.knowledge.runbook_distiller.make_llm_client",
            lambda: _BrokenLLM(),
        )
        steps = _make_steps("read_config", "read_logs", "run_ha_command")
        asyncio.run(
            distill_and_persist(
                trigger="repair",
                signature="repair:zha:device_offline",
                steps=steps,
                outcome="success",
                episode_id="ep7",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
            )
        )
        # No crash, no row inserted
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0
