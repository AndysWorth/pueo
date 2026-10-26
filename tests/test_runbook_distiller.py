"""Tests for utils/knowledge/runbook_distiller.py."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from pydantic import ValidationError

from utils.knowledge.runbook_distiller import (
    DistilledRunbook,
    InstanceFact,
    _build_prompt,
    _distillation_timeout_seconds,
    _fact_is_grounded,
    _format_step_trace,
    _get_existing_by_signature,
    _grounded_facts,
    _mark_distilled,
    _read_distillation_payload,
    _write_instance_facts,
    distill_and_persist,
    enqueue_distillation_after_verification,
    resolve_gap,
    write_gap,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step(
    name: str,
    step_number: int = 1,
    success: bool = True,
    output: str = "ok",
):
    from utils.agent.tool_registry import AgentStep, ToolCall, ToolResult

    return AgentStep(
        step_number=step_number,
        tool_call=ToolCall(name=name, arguments={}),
        tool_result=ToolResult(
            tool_name=name,
            success=success,
            output=output,
            error=None if success else "err",
        ),
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
        conn.execute(
            "CREATE TABLE session_outcomes ("
            "episode_id TEXT PRIMARY KEY, "
            "signature TEXT, "
            "session_type TEXT, "
            "subject_key TEXT, "
            "conclusion_json TEXT, "
            "distill_payload_json TEXT, "
            "created_at TEXT, "
            "verification TEXT, "
            "verified_at TEXT, "
            "verified_by TEXT, "
            "correction TEXT, "
            "distilled_at TEXT, "
            "card_id TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE agent_memory ("
            "id INTEGER PRIMARY KEY, key TEXT NOT NULL, content TEXT NOT NULL, "
            "source TEXT NOT NULL, ts REAL NOT NULL, signature TEXT)"
        )
        conn.commit()
    return db_path


def _insert_session(db_path, episode_id, signature="repair:zha:device_offline"):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_outcomes"
            " (episode_id, signature, distill_payload_json, created_at, verification)"
            " VALUES (?, ?, '{\"steps\": []}', datetime('now'), 'success')",
            (episode_id, signature),
        )
        conn.commit()


def _distilled_at(db_path, episode_id):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT distilled_at FROM session_outcomes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()[0]


def _memory_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT key, content, source, signature FROM agent_memory ORDER BY id"
        ).fetchall()


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
# _build_prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_includes_signature_and_outcome(self):
        prompt = _build_prompt(
            signature="repair:zha:device_offline",
            trigger="repair_poll",
            outcome="success",
            initial_context="ZHA device offline",
            episode_stub=None,
            existing_approach="",
            step_trace="  1. read_logs() → some log output",
        )
        assert "repair:zha:device_offline" in prompt
        assert "success" in prompt
        assert "ZHA device offline" in prompt

    def test_includes_terminal_tool_conclusions(self):
        stub = {
            "summary": "ZHA coordinator crashed",
            "recommendation": "Restart ZHA integration",
            "action_taken": "Ran ha integration reload",
        }
        prompt = _build_prompt(
            signature="sig",
            trigger="t",
            outcome="success",
            initial_context="ctx",
            episode_stub=stub,
            existing_approach="",
            step_trace="trace",
        )
        assert "ZHA coordinator crashed" in prompt
        assert "Restart ZHA integration" in prompt
        assert "Ran ha integration reload" in prompt

    def test_includes_existing_runbook_when_present(self):
        prompt = _build_prompt(
            signature="sig",
            trigger="t",
            outcome="success",
            initial_context="ctx",
            episode_stub=None,
            existing_approach="## When to use\nWhen ZHA crashes",
            step_trace="trace",
        )
        assert "refine, don't replace" in prompt
        assert "When ZHA crashes" in prompt

    def test_excludes_existing_section_when_empty(self):
        prompt = _build_prompt(
            signature="sig",
            trigger="t",
            outcome="success",
            initial_context="ctx",
            episode_stub=None,
            existing_approach="",
            step_trace="trace",
        )
        assert "refine, don't replace" not in prompt

    def test_initial_context_truncated_at_head(self):
        # First 50 chars are distinctive START_TOKEN, rest is padding, last 50 are END_TOKEN
        start = "START_TOKEN_AAAA"
        end = "END_TOKEN_ZZZZ"
        long_ctx = start + ("B" * 9_900) + end
        prompt = _build_prompt(
            signature="sig",
            trigger="t",
            outcome="success",
            initial_context=long_ctx,
            episode_stub=None,
            existing_approach="",
            step_trace="trace",
        )
        # Head strategy: start is present, tail end token is dropped
        assert start in prompt
        assert end not in prompt


# ---------------------------------------------------------------------------
# _distillation_timeout_seconds
# ---------------------------------------------------------------------------


class TestDistillationTimeoutSeconds:
    def test_returns_float(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        # No llm_calls table → falls back to default
        t = _distillation_timeout_seconds(db_path, "qwen2.5:7b")
        assert isinstance(t, float)
        assert t > 0

    def test_minimum_bound_respected(self, tmp_path, monkeypatch):
        import config as _cfg

        db_path = str(tmp_path / "t.db")
        monkeypatch.setattr(_cfg, "AGENT_PER_CALL_MIN_TIMEOUT_SECONDS", 60.0)
        monkeypatch.setattr(_cfg, "AGENT_PER_CALL_MAX_TIMEOUT_SECONDS", 1800.0)
        monkeypatch.setattr(_cfg, "AGENT_PER_CALL_TIMEOUT_FACTOR", 5.0)
        t = _distillation_timeout_seconds(db_path, "qwen2.5:7b")
        assert t >= 60.0


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

    async def chat(self, model, messages, options, format, think=None):  # noqa: A002
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
                verification="success",
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
                verification="success",
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
                verification="success",
            )
        )
        # Second distillation → merge (bumps version, updates approach + title)
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
                verification="success",
            )
        )

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT version, episode_refs, approach, title FROM agent_strategies"
            ).fetchall()
        assert len(rows) == 1  # still one row, not two
        assert rows[0][0] == 2  # version bumped
        refs = json.loads(rows[0][1])
        assert "ep4" in refs
        assert "ep5" in refs
        # approach and title should be updated to the latest distilled content
        assert "When to use" in rows[0][2]
        assert rows[0][3] == "Diagnosing core update risks"

    def test_injected_llm_client_used_preferentially(self, tmp_path, monkeypatch):
        """When llm_client is injected, make_llm_client must not be called."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()

        fallback_called = []

        def _bad_factory():
            fallback_called.append(True)
            raise RuntimeError("should not be called")

        monkeypatch.setattr(
            "utils.knowledge.runbook_distiller.make_llm_client",
            _bad_factory,
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
                episode_id="ep8",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
                llm_client=_FakeLLMWithRunbook(),
                verification="success",
            )
        )
        assert not fallback_called
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 1

    def test_episode_stub_included_in_prompt_sent_to_llm(self, tmp_path, monkeypatch):
        """episode_stub fields appear in the prompt passed to the LLM."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        captured_prompts: list[str] = []

        class _CapturingLLM:
            def __init__(self):
                self._rb = _FakeLLMWithRunbook()

            async def chat(
                self, model, messages, options, format, think=None
            ):  # noqa: A002
                for m in messages:
                    if m.get("role") == "user":
                        captured_prompts.append(m["content"])
                return await self._rb.chat(
                    model, messages, options, format, think=think
                )

            async def chat_with_tools(self, *a, **kw):  # pragma: no cover
                raise NotImplementedError

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
                episode_id="ep9",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
                initial_context="HA core update detected",
                episode_stub={
                    "summary": "Update applied cleanly",
                    "recommendation": "Monitor logs",
                },
                llm_client=_CapturingLLM(),
                verification="success",
            )
        )
        assert captured_prompts, "LLM should have been called"
        full_prompt = captured_prompts[0]
        assert "HA core update detected" in full_prompt
        assert "Update applied cleanly" in full_prompt
        assert "Monitor logs" in full_prompt

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
                verification="success",
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
                verification="success",
            )
        )
        # No crash, no row inserted
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# distill_and_persist — verification gating
# ---------------------------------------------------------------------------


class TestDistillVerificationGating:
    def test_unverified_success_skips(self, tmp_path):
        """Success with verification=None → skipped, no row written."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        steps = _make_steps("read_config", "read_logs", "run_ha_command")
        asyncio.run(
            distill_and_persist(
                trigger="repair",
                signature="repair:zha:device_offline",
                steps=steps,
                outcome="success",
                episode_id="ep_unverified",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
                # no verification= → skipped
            )
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0

    def test_failure_without_correction_skips(self, tmp_path):
        """Failure + verification="failure" but no correction → skipped."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        steps = _make_steps("read_config", "read_logs", "run_ha_command")
        asyncio.run(
            distill_and_persist(
                trigger="repair",
                signature="repair:zha:device_offline",
                steps=steps,
                outcome="failed",
                episode_id="ep_fail_nocorrect",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
                verification="failure",
                correction=None,
            )
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0

    def test_no_new_knowledge_skips_write(self, tmp_path, monkeypatch):
        """LLM returns no_new_knowledge=True → no row inserted."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()

        class _NoNewKnowledgeLLM:
            async def chat(
                self, model, messages, options, format, think=None
            ):  # noqa: A002
                rb = DistilledRunbook(
                    title="T",
                    when_to_use="W",
                    recommended_tools=["read_logs"],
                    hypotheses=["H"],
                    fix_summary="F",
                    no_new_knowledge=True,
                )
                return {"message": {"content": rb.model_dump_json()}}

            async def chat_with_tools(self, *a, **kw):  # pragma: no cover
                raise NotImplementedError

        steps = _make_steps("read_config", "read_logs", "run_ha_command")
        asyncio.run(
            distill_and_persist(
                trigger="repair",
                signature="repair:zha:device_offline",
                steps=steps,
                outcome="success",
                episode_id="ep_nnk",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
                verification="success",
                llm_client=_NoNewKnowledgeLLM(),
            )
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# enqueue_distillation_after_verification
# ---------------------------------------------------------------------------


class TestEnqueueDistillationAfterVerification:
    def test_returns_false_when_no_queue(self, tmp_path, monkeypatch):
        """No work queue → returns False without crashing."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        monkeypatch.setattr(
            "utils.agent.work_queue.get_work_queue_or_none",
            lambda: None,
            raising=False,
        )
        db_path = _make_db(tmp_path)
        result = asyncio.run(
            enqueue_distillation_after_verification(
                episode_id="ep1",
                verification="success",
                correction=None,
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
            )
        )
        assert result is False

    def test_returns_false_for_missing_payload(self, tmp_path, monkeypatch):
        """Payload not in DB → returns False."""
        from utils.agent.work_queue import PueoWorkQueue
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        q = PueoWorkQueue()
        monkeypatch.setattr(
            "utils.agent.work_queue.get_work_queue_or_none",
            lambda: q,
            raising=False,
        )
        db_path = _make_db(tmp_path)
        # No session_outcomes row → payload missing
        result = asyncio.run(
            enqueue_distillation_after_verification(
                episode_id="ep_missing",
                verification="success",
                correction=None,
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
            )
        )
        assert result is False

    def test_enqueues_when_payload_present(self, tmp_path, monkeypatch):
        """Valid payload in DB → submits WorkItem, returns True."""
        import json as _json

        from utils.agent.work_queue import PueoWorkQueue
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        q = PueoWorkQueue()
        monkeypatch.setattr(
            "utils.agent.work_queue.get_work_queue_or_none",
            lambda: q,
            raising=False,
        )
        db_path = _make_db(tmp_path)
        payload = _json.dumps(
            {
                "steps": [],
                "initial_context": "ctx",
                "episode_stub": None,
            }
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO session_outcomes"
                " (episode_id, signature, distill_payload_json, created_at)"
                " VALUES (?, ?, ?, datetime('now'))",
                ("ep_ok", "repair:zha:device_offline", payload),
            )
            conn.commit()

        result = asyncio.run(
            enqueue_distillation_after_verification(
                episode_id="ep_ok",
                verification="success",
                correction=None,
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
            )
        )
        assert result is True

    def test_returns_false_for_failure_without_correction(self, tmp_path, monkeypatch):
        """failure + no correction → returns False before reading payload."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        monkeypatch.setattr(
            "utils.agent.work_queue.get_work_queue_or_none",
            lambda: None,
            raising=False,
        )
        db_path = _make_db(tmp_path)
        result = asyncio.run(
            enqueue_distillation_after_verification(
                episode_id="ep_fail",
                verification="failure",
                correction=None,
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
            )
        )
        assert result is False


# ---------------------------------------------------------------------------
# InstanceFact schema
# ---------------------------------------------------------------------------


class TestInstanceFactSchema:
    def test_valid_construction(self):
        f = InstanceFact(fact="Board is a CM4 Yellow", evidence_step=2)
        assert f.evidence_step == 2

    def test_missing_evidence_step_rejected(self):
        with pytest.raises(ValidationError):
            InstanceFact(fact="Board is a CM4 Yellow")  # type: ignore[call-arg]

    def test_json_round_trip(self):
        rb = DistilledRunbook(
            title="T",
            when_to_use="W",
            recommended_tools=[],
            hypotheses=[],
            fix_summary="",
            instance_facts=[InstanceFact(fact="EEPROM 2021-12-02", evidence_step=1)],
        )
        restored = DistilledRunbook.model_validate_json(rb.model_dump_json())
        assert restored.instance_facts[0].fact == "EEPROM 2021-12-02"
        assert restored.instance_facts[0].evidence_step == 1


# ---------------------------------------------------------------------------
# Grounding check
# ---------------------------------------------------------------------------


class TestFactGrounding:
    def test_grounded_distinctive_tokens(self):
        evidence = "board: yellow\nbootloader eeprom current: 2021-12-02 (CM4)"
        assert _fact_is_grounded("CM4 bootloader EEPROM dated 2021-12-02", evidence)

    def test_invented_proper_noun_rejected(self):
        """The 9a05e6c4 case: an owner name that appears in no tool output."""
        evidence = (
            "Login attempt from 10.0.0.168, user agent Home Assistant/2026.10 iOS"
        )
        assert not _fact_is_grounded("Device at 10.0.0.168 is Robbie's phone", evidence)

    def test_matching_version_with_invented_claim_rejected(self):
        evidence = "mosquitto broker version 7.1.2 running"
        assert not _fact_is_grounded("Mosquitto 7.1.2 crashes nightly", evidence)
        assert _fact_is_grounded("Mosquitto broker 7.1.2 running", evidence)

    def test_missing_version_rejected(self):
        assert not _fact_is_grounded(
            "Mosquitto runs 7.1.2", "mosquitto broker version 7.1.1 running"
        )

    def test_content_words_need_majority(self):
        evidence = "the integration is downloaded but not set up"
        assert _fact_is_grounded("integration downloaded but not set up", evidence)
        assert not _fact_is_grounded(
            "integration crashes nightly during backups", evidence
        )

    def test_empty_fact_or_evidence(self):
        assert not _fact_is_grounded("", "anything")
        assert not _fact_is_grounded("something real", "   ")
        assert not _fact_is_grounded("the of a", "the of a")

    def test_grounded_facts_filters_by_step(self):
        failed = _make_step("read_logs", 2, success=False)
        failed.tool_result.error = "SSH connection refused by host"
        steps = [
            _make_step("run_ha_command", 1, output="board: yellow, CM4"),
            failed,
        ]
        facts = [
            InstanceFact(fact="Board is CM4", evidence_step=1),
            InstanceFact(fact="Board is CM4", evidence_step=9),  # no such step
            InstanceFact(fact="Owner is Robbie", evidence_step=1),  # ungrounded
            InstanceFact(fact="SSH connection refused", evidence_step=2),  # error text
        ]
        kept = _grounded_facts(facts, steps)
        assert [(f.fact, f.evidence_step) for f in kept] == [
            ("Board is CM4", 1),
            ("SSH connection refused", 2),
        ]


# ---------------------------------------------------------------------------
# Instance-fact persistence, distilled_at, trace, prompt variants
# ---------------------------------------------------------------------------


class TestInstanceFactPersistence:
    def test_write_upserts_by_signature_and_text(self, tmp_path):
        db_path = _make_db(tmp_path)
        f = InstanceFact(fact="Board is CM4", evidence_step=1)
        assert _write_instance_facts(db_path, "host:x:y", "ep1", [f]) == 1
        # Same fact, different spacing/case, later episode → replaces, not duplicates.
        g = InstanceFact(fact="board  is CM4", evidence_step=3)
        _write_instance_facts(db_path, "host:x:y", "ep2", [g])
        rows = _memory_rows(db_path)
        assert len(rows) == 1
        assert rows[0][1] == "board  is CM4"
        assert rows[0][2] == "distilled:ep2"
        assert rows[0][3] == "host:x:y"
        assert rows[0][0].startswith("fact_")

    def test_write_noop_without_facts_or_signature(self, tmp_path):
        db_path = _make_db(tmp_path)
        assert _write_instance_facts(db_path, "host:x:y", "ep1", []) == 0
        f = InstanceFact(fact="Board is CM4", evidence_step=1)
        assert _write_instance_facts(db_path, "", "ep1", [f]) == 0
        assert _memory_rows(db_path) == []


class TestMarkDistilled:
    def test_sets_timestamp(self, tmp_path):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep1")
        _mark_distilled(db_path, "ep1")
        assert _distilled_at(db_path, "ep1")

    def test_noop_without_ids(self, tmp_path):
        _mark_distilled("", "ep1")
        _mark_distilled(str(tmp_path / "x.db"), "")

    def test_db_error_is_logged_not_raised(self, tmp_path):
        # Directory path → sqlite cannot open it.
        _mark_distilled(str(tmp_path), "ep1")

    def test_read_payload_skips_distilled_rows(self, tmp_path):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep1")
        assert _read_distillation_payload(db_path, "ep1") is not None
        _mark_distilled(db_path, "ep1")
        assert _read_distillation_payload(db_path, "ep1") is None


class _FactsLLM:
    """Returns no_new_knowledge with one grounded and one invented fact."""

    def __init__(self, no_new_knowledge=True):
        self.prompts: list[str] = []
        self._nnk = no_new_knowledge

    async def chat(self, model, messages, options, format, think=None):  # noqa: A002
        self.prompts.append(messages[0]["content"])
        rb = DistilledRunbook(
            title="Firmware update blocked",
            when_to_use="W",
            recommended_tools=["run_ha_command"],
            hypotheses=[],
            fix_summary="",
            instance_facts=[
                InstanceFact(fact="CM4 EEPROM dated 2021-12-02", evidence_step=1),
                InstanceFact(fact="Robbie owns this board", evidence_step=1),
            ],
            no_new_knowledge=self._nnk,
        )
        return {"message": {"content": rb.model_dump_json()}}

    async def chat_with_tools(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


class TestDistillFactsAndDistilledAt:
    SIG = "host:resolution_issue:rpi_firmware_update_blocked"

    def _run(self, db_path, llm, episode_id="ep_rpi", verification="success"):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        steps = [
            _make_step(
                "run_ha_command",
                1,
                output="blocked_reason: eeprom_update_unavailable\n"
                "current: 2021-12-02 (CM4 eeprom)",
            )
        ]
        asyncio.run(
            distill_and_persist(
                trigger=self.SIG,
                signature=self.SIG,
                steps=steps,
                outcome="success",
                episode_id=episode_id,
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
                llm_client=llm,
                verification=verification,
            )
        )

    def test_no_new_knowledge_still_saves_grounded_facts(self, tmp_path):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep_rpi", self.SIG)
        self._run(db_path, _FactsLLM(no_new_knowledge=True))
        rows = _memory_rows(db_path)
        assert [r[1] for r in rows] == ["CM4 EEPROM dated 2021-12-02"]
        assert rows[0][3] == self.SIG
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0
        assert _distilled_at(db_path, "ep_rpi")

    def test_candidate_approach_lists_only_grounded_facts(self, tmp_path):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep_rpi", self.SIG)
        self._run(db_path, _FactsLLM(no_new_knowledge=False))
        with sqlite3.connect(db_path) as conn:
            approach = conn.execute("SELECT approach FROM agent_strategies").fetchone()[
                0
            ]
        assert "CM4 EEPROM dated 2021-12-02" in approach
        assert "Robbie" not in approach
        assert _distilled_at(db_path, "ep_rpi")

    def test_fact_write_failure_does_not_block(self, tmp_path, monkeypatch):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep_rpi", self.SIG)

        def _boom(*a, **kw):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(
            "utils.knowledge.runbook_distiller._write_instance_facts", _boom
        )
        self._run(db_path, _FactsLLM(no_new_knowledge=False))
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 1
        assert _distilled_at(db_path, "ep_rpi")

    def test_llm_failure_marks_distilled(self, tmp_path):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep_rpi", self.SIG)

        class _BrokenLLM:
            async def chat(self, **kw):
                raise RuntimeError("Ollama down")

        self._run(db_path, _BrokenLLM())
        assert _distilled_at(db_path, "ep_rpi")

    def test_gating_skip_does_not_mark_distilled(self, tmp_path):
        """Failure without correction never reaches the LLM and stays undistilled."""
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep_rpi", self.SIG)
        self._run(db_path, _FactsLLM(), verification="failure")
        assert _distilled_at(db_path, "ep_rpi") is None


class TestTraceAndPromptVariants:
    def test_trace_keeps_long_output_within_budget(self):
        long_out = "HEAD " + "x" * 50_000 + " TAIL"
        trace = _format_step_trace([_make_step("read_logs", 1, output=long_out)])
        assert "HEAD" in trace and "TAIL" in trace
        assert len(trace) < 20_000

    def test_trace_splits_budget_across_steps(self):
        steps = [_make_step("read_logs", i, output="y" * 10_000) for i in range(1, 5)]
        trace = _format_step_trace(steps)
        assert len(trace) < 4 * 10_000

    def test_trace_failed_and_empty_markers(self):
        trace = _format_step_trace(
            [
                _make_step("read_logs", 1, success=False),
                _make_step("read_config", 2, output="  "),
            ]
        )
        assert "[FAILED] err" in trace
        assert "[EMPTY]" in trace

    def _prompt(self, seed_text=""):
        return _build_prompt(
            signature="update:addon:x",
            trigger="t",
            outcome="success",
            initial_context="ctx",
            episode_stub=None,
            existing_approach="",
            step_trace="trace",
            seed_text=seed_text,
        )

    def test_no_seed_prompt_asks_for_signature_level_runbook(self):
        prompt = self._prompt()
        assert "No seed runbook exists" in prompt
        assert "not\nfor one specific version" not in prompt
        assert "delta from the seed" not in prompt
        assert "evidence_step" in prompt

    def test_seed_prompt_asks_for_delta(self):
        prompt = self._prompt(seed_text="# Seed\nDo X")
        assert "delta from the seed" in prompt
        assert "No seed runbook exists" not in prompt
        assert "evidence_step" in prompt


# ---------------------------------------------------------------------------
# ProceduralLesson / ProceduralLessons schema
# ---------------------------------------------------------------------------


class TestProceduralLessonSchema:
    def test_valid_construction(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            ProceduralLessons,
        )

        lesson = ProceduralLesson(
            lesson="get_entity_history failed: REST client not available",
            evidence_step=3,
        )
        assert lesson.evidence_step == 3
        pl = ProceduralLessons(lessons=[lesson])
        assert len(pl.lessons) == 1

    def test_missing_evidence_step_rejected(self):
        from pydantic import ValidationError

        from utils.knowledge.runbook_distiller import ProceduralLesson

        with pytest.raises(ValidationError):
            ProceduralLesson(lesson="some lesson")  # type: ignore[call-arg]

    def test_json_round_trip(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            ProceduralLessons,
        )

        pl = ProceduralLessons(
            lessons=[
                ProceduralLesson(
                    lesson="call get_logbook after get_entity_history", evidence_step=2
                )
            ]
        )
        restored = ProceduralLessons.model_validate_json(pl.model_dump_json())
        assert restored.lessons[0].lesson == pl.lessons[0].lesson
        assert restored.lessons[0].evidence_step == 2

    def test_empty_lessons_default(self):
        from utils.knowledge.runbook_distiller import ProceduralLessons

        pl = ProceduralLessons()
        assert pl.lessons == []


# ---------------------------------------------------------------------------
# _grounded_lessons
# ---------------------------------------------------------------------------


class TestGroundedLessons:
    def test_grounded_lesson_kept(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _grounded_lessons,
        )

        step = _make_step("get_entity_history", 3, success=False, output="")
        step.tool_result.error = "REST client not available"
        lessons = [
            ProceduralLesson(
                lesson="get_entity_history failed: REST client not available",
                evidence_step=3,
            ),
        ]
        kept = _grounded_lessons(lessons, [step])
        assert len(kept) == 1

    def test_ungrounded_lesson_rejected(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _grounded_lessons,
        )

        step = _make_step("get_entity_history", 3, success=True, output="state: on")
        lessons = [
            ProceduralLesson(
                lesson="device was offline because Wi-Fi failed", evidence_step=3
            ),
        ]
        kept = _grounded_lessons(lessons, [step])
        assert len(kept) == 0

    def test_missing_step_rejected(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _grounded_lessons,
        )

        step = _make_step("read_logs", 1, output="some log")
        lessons = [
            ProceduralLesson(lesson="some lesson about step 99", evidence_step=99),
        ]
        kept = _grounded_lessons(lessons, [step])
        assert len(kept) == 0

    def test_grounded_against_args(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _grounded_lessons,
        )

        step = _make_step("run_ha_command", 2, output="error: command not found")
        # lesson cites a word that appears in args OR output
        step.tool_call.arguments = {"cmd": "ha apps restart mosquitto"}
        lessons = [
            ProceduralLesson(
                lesson="run_ha_command failed: command not found", evidence_step=2
            ),
        ]
        kept = _grounded_lessons(lessons, [step])
        assert len(kept) == 1


# ---------------------------------------------------------------------------
# _split_procedure_notes / _merge_notes_text
# ---------------------------------------------------------------------------


class TestSplitProcedureNotes:
    def test_no_section_returns_empty_notes(self):
        from utils.knowledge.runbook_distiller import _split_procedure_notes

        approach = "## When to use\nWhen ZHA crashes"
        main, notes = _split_procedure_notes(approach)
        assert notes == ""
        assert main == approach

    def test_splits_at_header(self):
        from utils.knowledge.runbook_distiller import _split_procedure_notes

        approach = (
            "## When to use\nWhen ZHA crashes\n\n## Procedure notes\n- read_logs first"
        )
        main, notes = _split_procedure_notes(approach)
        assert "Procedure notes" not in main
        assert "read_logs first" in notes

    def test_preserves_section_after_notes(self):
        from utils.knowledge.runbook_distiller import _split_procedure_notes

        approach = (
            "## When to use\nWhen X\n\n"
            "## Procedure notes\n- lesson A\n\n"
            "## Fix summary\nDid Y"
        )
        main, notes = _split_procedure_notes(approach)
        assert "Fix summary" in main
        assert "lesson A" in notes
        assert "Procedure notes" not in main


class TestMergeNotesText:
    def test_adds_new_lessons(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _merge_notes_text,
        )

        lessons = [ProceduralLesson(lesson="call A before B", evidence_step=1)]
        result = _merge_notes_text("", lessons)
        assert "call A before B" in result

    def test_deduplicates_case_insensitive(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _merge_notes_text,
        )

        existing = "- Call A before B"
        lessons = [ProceduralLesson(lesson="call A before B", evidence_step=2)]
        result = _merge_notes_text(existing, lessons)
        assert result.count("call A before B") + result.count("Call A before B") == 1

    def test_caps_at_max(self):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _PROCEDURE_MAX_NOTES,
            _merge_notes_text,
        )

        existing = "\n".join(f"- note {i}" for i in range(_PROCEDURE_MAX_NOTES))
        new_lessons = [ProceduralLesson(lesson="overflow note", evidence_step=1)]
        result = _merge_notes_text(existing, new_lessons)
        bullet_count = result.count("\n- ") + (1 if result.startswith("- ") else 0)
        assert bullet_count <= _PROCEDURE_MAX_NOTES
        assert "overflow note" not in result


# ---------------------------------------------------------------------------
# _build_procedure_prompt
# ---------------------------------------------------------------------------


class TestBuildProcedurePrompt:
    def test_includes_signature_and_outcome(self):
        from utils.knowledge.runbook_distiller import _build_procedure_prompt

        prompt = _build_procedure_prompt(
            signature="device:offline:lifx",
            outcome="monitoring",
            step_trace="  1. read_logs() → some output",
        )
        assert "device:offline:lifx" in prompt
        assert "monitoring" in prompt

    def test_no_verdict_language(self):
        from utils.knowledge.runbook_distiller import _build_procedure_prompt

        prompt = _build_procedure_prompt(
            signature="sig",
            outcome="success",
            step_trace="trace",
        )
        assert "Do NOT state a root cause" in prompt
        assert "verdict" in prompt.lower() or "root cause" in prompt.lower()

    def test_asks_for_tool_mechanics(self):
        from utils.knowledge.runbook_distiller import _build_procedure_prompt

        prompt = _build_procedure_prompt(
            signature="sig",
            outcome="success",
            step_trace="trace",
        )
        assert "failed" in prompt.lower()
        assert "redundant" in prompt.lower()
        assert "step" in prompt.lower()


# ---------------------------------------------------------------------------
# _persist_procedure_notes
# ---------------------------------------------------------------------------


class TestPersistProcedureNotes:
    def test_creates_candidate_when_none_exists(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _persist_procedure_notes,
        )

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        lessons = [
            ProceduralLesson(lesson="REST client missing at step 3", evidence_step=1)
        ]
        ok = _persist_procedure_notes(db_path, "device:offline:lifx", lessons, store)
        assert ok
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT runbook_state, approach FROM agent_strategies"
            ).fetchone()
        assert row[0] == "candidate"
        assert "REST client missing" in row[1]
        assert "## Procedure notes" in row[1]

    def test_merges_into_existing_candidate(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _persist_procedure_notes,
        )

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        # Insert an existing candidate
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO agent_strategies"
                " (id, title, trigger_pattern, approach, runbook_state, created_at, signature)"
                " VALUES ('cand1', 'Test', 'trigger', '## When to use\nWhen X', 'candidate', datetime('now'), 'device:offline:lifx')"
            )
            conn.commit()
        lessons = [
            ProceduralLesson(
                lesson="get_entity_history works for lifx", evidence_step=2
            )
        ]
        ok = _persist_procedure_notes(db_path, "device:offline:lifx", lessons, store)
        assert ok
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, version, approach FROM agent_strategies WHERE id='cand1'"
            ).fetchone()
        assert row[0] == "cand1"
        assert row[1] == 2  # version bumped
        assert "get_entity_history works for lifx" in row[2]
        assert "## Procedure notes" in row[2]

    def test_deduplicates_notes_across_calls(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            _persist_procedure_notes,
        )

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        lessons = [ProceduralLesson(lesson="call read_logs first", evidence_step=1)]
        _persist_procedure_notes(db_path, "test:sig:x", lessons, store)
        _persist_procedure_notes(db_path, "test:sig:x", lessons, store)  # same lesson
        with sqlite3.connect(db_path) as conn:
            approach = conn.execute("SELECT approach FROM agent_strategies").fetchone()[
                0
            ]
        assert approach.count("call read_logs first") == 1


# ---------------------------------------------------------------------------
# distill_procedure
# ---------------------------------------------------------------------------


class _FakeLLMWithLessons:
    def __init__(self, lessons=None):
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            ProceduralLessons,
        )

        self._result = ProceduralLessons(
            lessons=lessons
            or [
                ProceduralLesson(
                    lesson="get_entity_history failed: REST client not available",
                    evidence_step=1,
                )
            ]
        )

    async def chat(self, model, messages, options, format, think=None):  # noqa: A002
        return {"message": {"content": self._result.model_dump_json()}}

    async def chat_with_tools(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


class TestDistillProcedure:
    SIG = "device:offline:lifx"

    def _make_step_rest_fail(self):
        s = _make_step("get_entity_history", 1, success=False)
        s.tool_result.error = "REST client not available"
        return s

    def test_grounded_lessons_written(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import distill_procedure

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()
        steps = [self._make_step_rest_fail()]

        asyncio.run(
            distill_procedure(
                signature=self.SIG,
                steps=steps,
                outcome="monitoring",
                episode_id="ep_proc1",
                db_path=db_path,
                knowledge_store=store,
                model="qwen2.5:7b",
                llm_client=_FakeLLMWithLessons(),
            )
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT runbook_state, approach FROM agent_strategies"
            ).fetchone()
        assert row is not None
        assert row[0] == "candidate"
        assert "REST client not available" in row[1]

    def test_no_distilled_at_stamped(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import distill_procedure

        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep_proc2", self.SIG)

        asyncio.run(
            distill_procedure(
                signature=self.SIG,
                steps=[self._make_step_rest_fail()],
                outcome="success",
                episode_id="ep_proc2",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
                llm_client=_FakeLLMWithLessons(),
            )
        )

        # distilled_at must NOT be set — that's the verdict lane's job
        assert _distilled_at(db_path, "ep_proc2") is None

    def test_llm_failure_does_not_crash(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import distill_procedure

        db_path = _make_db(tmp_path)

        class _BrokenLLM:
            async def chat(self, **kw):
                raise RuntimeError("Ollama down")

        asyncio.run(
            distill_procedure(
                signature=self.SIG,
                steps=[self._make_step_rest_fail()],
                outcome="success",
                episode_id="ep_proc3",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
                llm_client=_BrokenLLM(),
            )
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0  # nothing written, no crash

    def test_ungrounded_lessons_skipped(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import (
            ProceduralLesson,
            distill_procedure,
        )

        db_path = _make_db(tmp_path)
        # The LLM returns a lesson that cites an invented step
        ungrounded_client = _FakeLLMWithLessons(
            lessons=[
                ProceduralLesson(lesson="invented ungrounded claim", evidence_step=99)
            ]
        )
        asyncio.run(
            distill_procedure(
                signature=self.SIG,
                steps=[_make_step("read_logs", 1, output="some log")],
                outcome="success",
                episode_id="ep_proc4",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
                llm_client=ungrounded_client,
            )
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0  # no lessons survived grounding


# ---------------------------------------------------------------------------
# enqueue_procedural_distillation
# ---------------------------------------------------------------------------


class TestEnqueueProceduralDistillation:
    def test_returns_false_when_no_queue(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import enqueue_procedural_distillation

        monkeypatch.setattr(
            "utils.agent.work_queue.get_work_queue_or_none",
            lambda: None,
            raising=False,
        )
        db_path = _make_db(tmp_path)
        result = asyncio.run(
            enqueue_procedural_distillation(
                episode_id="ep1",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
            )
        )
        assert result is False

    def test_returns_false_for_missing_payload(self, tmp_path, monkeypatch):
        from utils.agent.work_queue import PueoWorkQueue
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import enqueue_procedural_distillation

        q = PueoWorkQueue()
        monkeypatch.setattr(
            "utils.agent.work_queue.get_work_queue_or_none",
            lambda: q,
            raising=False,
        )
        db_path = _make_db(tmp_path)
        result = asyncio.run(
            enqueue_procedural_distillation(
                episode_id="ep_missing",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
            )
        )
        assert result is False

    def test_enqueues_when_payload_present(self, tmp_path, monkeypatch):
        import json as _json

        from utils.agent.work_queue import PueoWorkQueue
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import enqueue_procedural_distillation

        q = PueoWorkQueue()
        monkeypatch.setattr(
            "utils.agent.work_queue.get_work_queue_or_none",
            lambda: q,
            raising=False,
        )
        db_path = _make_db(tmp_path)
        payload = _json.dumps(
            {"steps": [], "initial_context": "ctx", "episode_stub": None}
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO session_outcomes"
                " (episode_id, signature, distill_payload_json, created_at)"
                " VALUES (?, ?, ?, datetime('now'))",
                ("ep_ok", "device:offline:lifx", payload),
            )
            conn.commit()

        result = asyncio.run(
            enqueue_procedural_distillation(
                episode_id="ep_ok",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
            )
        )
        assert result is True


# ---------------------------------------------------------------------------
# Relaxed lesson grounding, deterministic extractor, tool quirks (#991)
# ---------------------------------------------------------------------------


def _step(name, n, args=None, output="ok", success=True, error=None, awaiting=False):
    from utils.agent.tool_registry import AgentStep, ToolCall, ToolResult

    return AgentStep(
        step_number=n,
        tool_call=ToolCall(name=name, arguments=args or {}),
        tool_result=ToolResult(
            tool_name=name,
            success=success,
            output=output,
            error=error,
            awaiting_approval=awaiting,
        ),
        timestamp=0.0,
    )


class TestLessonIsGrounded:
    def _steps(self):
        return [
            _step("read_logs", 1, {"lines": 40}, output="ERROR noaa_coops traceback"),
            _step("get_entity_history", 2, {"entity_id": "light.top", "hours": 48}),
            _step("get_entity_history", 3, {"entity_id": "light.top", "hours": 48}),
            _step(
                "get_integration_diagnostics",
                4,
                {"domain_or_entry_id": "cast"},
                output="",
                success=False,
                error="cast does not provide diagnostics (HTTP 404)",
            ),
            _step("finish_device_availability", 5, output=""),
        ]

    def test_meta_lesson_accepted_without_content_words(self):
        from utils.knowledge.runbook_distiller import _lesson_is_grounded

        steps = self._steps()
        lesson = "Step 2 and step 3 were redundant: get_entity_history called 2x"
        assert _lesson_is_grounded(lesson, steps[1], steps)

    def test_step_reference_widens_evidence(self):
        from utils.knowledge.runbook_distiller import _lesson_is_grounded

        steps = self._steps()
        # light.top lives in step 2's args; the lesson cites step 4 but mentions step 2.
        lesson = "get_integration_diagnostics failed (404) after step 2 used light.top"
        assert _lesson_is_grounded(lesson, steps[3], steps)
        assert not _lesson_is_grounded(
            "get_integration_diagnostics failed (404) for light.top", steps[3], steps
        )

    def test_lesson_naming_no_tool_rejected(self):
        from utils.knowledge.runbook_distiller import _lesson_is_grounded

        steps = self._steps()
        assert not _lesson_is_grounded("Step 4 failed with 404", steps[3], steps)
        assert not _lesson_is_grounded("", steps[3], steps)

    def test_terminal_tool_does_not_count_as_named_tool(self):
        from utils.knowledge.runbook_distiller import _lesson_is_grounded

        steps = self._steps()
        assert not _lesson_is_grounded(
            "finish_device_availability returned EMPTY output", steps[4], steps
        )

    def test_invented_token_rejected(self):
        from utils.knowledge.runbook_distiller import _lesson_is_grounded

        steps = self._steps()
        assert not _lesson_is_grounded(
            "get_integration_diagnostics needs entry 01ABC", steps[3], steps
        )

    def test_log_reader_content_rejected_but_args_accepted(self):
        from utils.knowledge.runbook_distiller import _lesson_is_grounded

        steps = self._steps()
        assert not _lesson_is_grounded(
            "read_logs output contained a noaa_coops traceback", steps[0], steps
        )
        assert _lesson_is_grounded(
            "read_logs with lines 40 returned unrelated tracebacks", steps[0], steps
        )


class TestExtractProcedureFacts:
    def test_failure_with_httpx_noise_normalised(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        steps = [
            _step(
                "get_integration_diagnostics",
                2,
                {"domain_or_entry_id": "01JJ5506C3JCZNYTNHMPBZRD9T"},
                output="",
                success=False,
                error=(
                    "Diagnostics fetch failed: Client error '404 Not Found' for url "
                    "'http://ha:8123/api/diagnostics/config_entry/01JJ5506C3JCZNYTNHMPBZRD9T'"
                    "\nFor more information check: https://developer.mozilla.org/404"
                ),
            )
        ]
        notes = extract_procedure_facts(steps)
        assert [n.lesson for n in notes] == [
            "`get_integration_diagnostics(<id>)` failed: "
            "Diagnostics fetch failed: HTTP 404 Not Found"
        ]
        assert notes[0].evidence_step == 2

    def test_correction_recorded(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        steps = [
            _step(
                "get_entity_history",
                2,
                {"entity_id": "humidy", "hours": 24},
                output="",
                success=False,
                error="history fetch failed: 400",
            ),
            _step(
                "get_entity_history",
                3,
                {"entity_id": "humidifier.humidy", "hours": 24},
                output="2026-10-26 unavailable",
            ),
        ]
        (note,) = extract_procedure_facts(steps)
        assert note.lesson == (
            "`get_entity_history(entity_id=humidy, hours=24)` failed (history fetch "
            "failed: 400); `get_entity_history(entity_id=humidifier.humidy, hours=24)` "
            "worked"
        )

    def test_unrelated_command_is_not_a_correction(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        steps = [
            _step(
                "run_ha_command",
                1,
                {"command": "ha apps info netalertx_full_access"},
                output="",
                success=False,
                error="exit 1: Error: App netalertx_full_access does not exist",
            ),
            _step("run_ha_command", 2, {"command": "ha apps list"}, output="apps"),
            _step("search_integrations", 3, {"query": "x"}, output="Installed: x"),
        ]
        (note,) = extract_procedure_facts(steps)
        assert note.lesson.endswith(
            "failed: exit 1: Error: App netalertx_full_access does not exist"
        )

    def test_correction_requires_same_arg_names(self):
        from utils.knowledge.runbook_distiller import _is_correction
        from utils.agent.tool_registry import ToolCall

        bad = ToolCall(name="t", arguments={"a": "x"})
        assert not _is_correction(bad, ToolCall(name="t", arguments={"b": "x"}))
        assert not _is_correction(bad, ToolCall(name="u", arguments={"a": "x.y"}))
        assert not _is_correction(bad, ToolCall(name="t", arguments={"a": "x"}))
        assert _is_correction(
            ToolCall(name="t", arguments={"a": "x", "h": 1}),
            ToolCall(name="t", arguments={"a": "d.x", "h": 1}),
        )

    def test_skips_environment_approval_terminal_and_empty_log_reads(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        steps = [
            _step(
                "get_logbook",
                1,
                output="",
                success=False,
                error="REST client not available",
            ),
            _step(
                "reload_integration", 2, output="queued", success=False, awaiting=True
            ),
            _step("finish_device_availability", 3, output=""),
            _step("search_log", 4, {"pattern": "tuya"}, output="(no matches)"),
            _step("read_logs", 5, output=""),
        ]
        assert extract_procedure_facts(steps) == []

    def test_empty_result_and_repeat_ordering(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        steps = [
            _step(
                "search_integrations",
                1,
                {"query": "top"},
                output="No integration named 'top'",
            ),
            _step(
                "get_entity_history",
                2,
                {"entity_id": "light.top", "reason": "a"},
                output="data",
            ),
            _step(
                "get_entity_history",
                3,
                {"entity_id": "light.top", "reason": "b"},
                output="data",
            ),
            _step(
                "fetch_url",
                4,
                {"url": "https://x"},
                output="",
                success=False,
                error="HTTP Error 404: Not Found",
            ),
        ]
        lessons = [n.lesson for n in extract_procedure_facts(steps)]
        assert lessons == [
            "`fetch_url(https://x)` failed: HTTP Error 404: Not Found",
            "`get_entity_history(light.top)` was called 2× with identical arguments; "
            "reuse the first result",
            "`search_integrations(top)` returned no results",
        ]

    def test_failure_without_error_text(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        (note,) = extract_procedure_facts(
            [_step("read_file", 1, {"path": "/x"}, output="", success=False)]
        )
        assert note.lesson == "`read_file(/x)` failed: (no error text)"

    def test_long_output_starting_with_no_is_not_empty(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        out = "No issues, but here is a lot of detail " + "x" * 300
        assert extract_procedure_facts([_step("get_text", 1, output=out)]) == []


def _prefs_db(tmp_path):
    db = str(tmp_path / "prefs.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    return db


def _fail_step(n=1, domain="cast"):
    return _step(
        "get_integration_diagnostics",
        n,
        {"domain_or_entry_id": domain},
        output="",
        success=False,
        error=f"Diagnostics fetch failed: HTTP 404 for {domain}",
    )


class TestRecordToolQuirks:
    def test_counts_once_per_session_and_accumulates_signatures(self, tmp_path):
        from utils.knowledge.runbook_distiller import _record_tool_quirks

        db = _prefs_db(tmp_path)
        steps = [_fail_step(1), _fail_step(2)]
        assert _record_tool_quirks(db, "device:offline:cast", steps) == 1
        _record_tool_quirks(db, "device:offline:cast", steps)
        _record_tool_quirks(db, "device:offline:post_restart", steps)
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT key, value FROM user_prefs").fetchall()
        assert len(rows) == 1
        assert rows[0][0].startswith("procedure_quirk:")
        q = json.loads(rows[0][1])
        assert q["count"] == 3
        assert q["signatures"] == ["device:offline:cast", "device:offline:post_restart"]
        assert q["tool"] == "get_integration_diagnostics"
        assert q["example"] == "get_integration_diagnostics(cast)"

    def test_no_problems_writes_nothing(self, tmp_path):
        from utils.knowledge.runbook_distiller import _record_tool_quirks

        assert _record_tool_quirks(_prefs_db(tmp_path), "s", [_step("x", 1)]) == 0

    def test_missing_table_returns_zero(self, tmp_path):
        from utils.knowledge.runbook_distiller import _record_tool_quirks

        assert _record_tool_quirks(str(tmp_path / "none.db"), "s", [_fail_step()]) == 0


class TestDistillProcedureExtractor:
    SIG = "device:offline:cast"

    def _db(self, tmp_path):
        db = _make_db(tmp_path)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
        return db

    def test_extracted_notes_kept_when_llm_fails(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import distill_procedure

        class _BrokenLLM:
            async def chat(self, **kw):
                raise RuntimeError("Ollama down")

        db = self._db(tmp_path)
        asyncio.run(
            distill_procedure(
                signature=self.SIG,
                steps=[_fail_step()],
                outcome="monitoring",
                episode_id="ep_x1",
                db_path=db,
                knowledge_store=FakeKnowledgeStore(),
                model="m",
                llm_client=_BrokenLLM(),
            )
        )
        with sqlite3.connect(db) as conn:
            approach = conn.execute("SELECT approach FROM agent_strategies").fetchone()[
                0
            ]
            quirks = conn.execute(
                "SELECT COUNT(*) FROM user_prefs WHERE key LIKE 'procedure_quirk:%'"
            ).fetchone()[0]
        assert "`get_integration_diagnostics(cast)` failed" in approach
        assert quirks == 1

    def test_prompt_lists_extracted_notes(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import distill_procedure

        prompts: list[str] = []

        class _Capture(_FakeLLMWithLessons):
            async def chat(
                self, model, messages, options, format, think=None
            ):  # noqa: A002
                prompts.append(messages[0]["content"])
                return await super().chat(model, messages, options, format, think)

        asyncio.run(
            distill_procedure(
                signature=self.SIG,
                steps=[_fail_step()],
                outcome="monitoring",
                episode_id="ep_x2",
                db_path=self._db(tmp_path),
                knowledge_store=FakeKnowledgeStore(),
                model="m",
                llm_client=_Capture(lessons=[]),
            )
        )
        assert "Already recorded (do not repeat these):" in prompts[0]
        assert "`get_integration_diagnostics(cast)` failed" in prompts[0]


class TestBuildProcedurePromptRules:
    def test_forbids_log_restatement_and_requires_tool_name(self):
        from utils.knowledge.runbook_distiller import _build_procedure_prompt

        prompt = _build_procedure_prompt(
            signature="s", outcome="success", step_trace="t"
        )
        assert "Do NOT restate what a log or tool output said" in prompt
        assert "Name the tool in every lesson" in prompt
        assert "Already recorded" not in prompt
