"""Tests for utils/knowledge/runbook_distiller.py."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from pydantic import ValidationError

from utils.knowledge.runbook_distiller import (
    DeltaItem,
    DistilledDelta,
    InstanceFact,
    _build_prompt,
    _distillation_timeout_seconds,
    _fact_is_grounded,
    _format_step_trace,
    _grounded_facts,
    _is_alpha_hyphenated,
    _is_pueo_mechanics,
    _mark_distilled,
    _pitfall_step_valid,
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
            "source TEXT NOT NULL, ts REAL NOT NULL, signature TEXT, subject_key TEXT)"
        )
        conn.execute(
            "CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        from agents.ha_agent_advanced import _migrate_v42, _migrate_v44

        _migrate_v42(conn.cursor())
        _migrate_v44(conn.cursor())
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

    def test_empty_delta_skips_write(self, tmp_path, monkeypatch):
        """LLM returns no rules or pitfalls → no entry, no runbook row."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db_path = _make_db(tmp_path)
        store = FakeKnowledgeStore()

        class _NoNewKnowledgeLLM:
            async def chat(
                self, model, messages, options, format, think=None
            ):  # noqa: A002
                return {"message": {"content": DistilledDelta().model_dump_json()}}

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
        rb = DistilledDelta(
            instance_facts=[InstanceFact(fact="EEPROM 2021-12-02", evidence_step=1)],
        )
        restored = DistilledDelta.model_validate_json(rb.model_dump_json())
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
    """Returns one grounded and one invented fact, plus a grounded rule if asked."""

    RULE = "Firmware update stays blocked when eeprom_update_unavailable"

    def __init__(self, with_rule=False):
        self.prompts: list[str] = []
        self._with_rule = with_rule

    async def chat(self, model, messages, options, format, think=None):  # noqa: A002
        self.prompts.append(messages[0]["content"])
        delta = DistilledDelta(
            decision_rules=(
                [DeltaItem(text=self.RULE, evidence_step=1)] if self._with_rule else []
            ),
            instance_facts=[
                InstanceFact(fact="CM4 EEPROM dated 2021-12-02", evidence_step=1),
                InstanceFact(fact="Robbie owns this board", evidence_step=1),
            ],
        )
        return {"message": {"content": delta.model_dump_json()}}

    async def chat_with_tools(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


class TestDistillFactsAndDistilledAt:
    SIG = "host:resolution_issue:rpi_firmware_update_blocked"

    def _run(
        self,
        db_path,
        llm,
        episode_id="ep_rpi",
        verification="success",
        deviated_from_runbook=None,
    ):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        steps = [
            _make_step(
                "run_ha_command",
                1,
                output="firmware update blocked\n"
                "blocked_reason: eeprom_update_unavailable\n"
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
                deviated_from_runbook=deviated_from_runbook,
            )
        )

    def test_empty_delta_still_saves_grounded_facts(self, tmp_path):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep_rpi", self.SIG)
        self._run(db_path, _FactsLLM(with_rule=False))
        rows = _memory_rows(db_path)
        assert [r[1] for r in rows] == ["CM4 EEPROM dated 2021-12-02"]
        assert rows[0][3] == self.SIG
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0
        assert _distilled_at(db_path, "ep_rpi")

    def test_rule_rendered_and_facts_kept_out_of_approach(self, tmp_path):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep_rpi", self.SIG)
        self._run(
            db_path,
            _FactsLLM(with_rule=True),
            deviated_from_runbook={"rule": "expected X", "why": "Y was better"},
        )
        with sqlite3.connect(db_path) as conn:
            approach = conn.execute("SELECT approach FROM agent_strategies").fetchone()[
                0
            ]
        assert _FactsLLM.RULE in approach
        # Facts live in agent_memory ([INSTANCE FACT]), not in the runbook text.
        assert "CM4 EEPROM" not in approach
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
        self._run(
            db_path,
            _FactsLLM(with_rule=True),
            deviated_from_runbook={"rule": "expected X", "why": "Y was better"},
        )
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

    def _prompt(self, seed_text="", known_entries=None, correction=None):
        return _build_prompt(
            signature="update:addon:x",
            trigger="t",
            outcome="success",
            initial_context="ctx",
            episode_stub=None,
            step_trace="trace",
            seed_text=seed_text,
            known_entries=known_entries,
            correction=correction,
        )

    def test_prompt_without_seed_or_entries(self):
        prompt = self._prompt()
        assert "Seed runbook for this symptom" not in prompt
        assert "Already learned" not in prompt
        assert "no\nversion numbers" not in prompt
        assert "no version numbers" in prompt.replace("\n", " ")
        assert "evidence_step" in prompt

    def test_prompt_lists_seed_and_known_entries(self):
        prompt = self._prompt(
            seed_text="# Seed\nDo X", known_entries=["Rule A"], correction="Do Y"
        )
        assert "Seed runbook for this symptom:\n# Seed\nDo X" in prompt
        assert "Already learned for this signature:\n- Rule A" in prompt
        assert "User correction" in prompt and "Do Y" in prompt
        # No whole-runbook fields are requested any more.
        assert "title" not in prompt.lower().split("tool trace")[1]

    def test_prompt_includes_terminal_conclusions_and_head_context(self):
        start, end = "START_TOKEN_AAAA", "END_TOKEN_ZZZZ"
        prompt = _build_prompt(
            signature="sig",
            trigger="t",
            outcome="success",
            initial_context=start + ("B" * 9_900) + end,
            episode_stub={
                "summary": "ZHA coordinator crashed",
                "recommendation": "Restart ZHA integration",
            },
            step_trace="trace",
            verification="success",
        )
        assert "ZHA coordinator crashed" in prompt
        assert "Restart ZHA integration" in prompt
        assert "success (verified: success)" in prompt
        assert start in prompt and end not in prompt


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


# ---------------------------------------------------------------------------
# Atomic runbook entries (ADR 051)
# ---------------------------------------------------------------------------

_UPDATE_SIG = "update:core:ha"
_RULE = "When configuration.yaml uses keep_days under recorder, rename it first"


def _update_steps():
    return [
        _step(
            "get_update_release_notes",
            1,
            {"version": "2026.11"},
            output="Breaking: recorder option keep_days renamed to purge_keep_days",
        ),
        _step(
            "check_config_against_breaking_change",
            2,
            output="configuration.yaml uses keep_days under recorder",
        ),
    ]


class _DeltaLLM:
    """Returns a fixed DistilledDelta and records each prompt."""

    def __init__(self, rules=(), pitfalls=()):
        self.prompts: list[str] = []
        self._delta = DistilledDelta(
            decision_rules=[DeltaItem(text=t, evidence_step=2) for t in rules],
            pitfalls=[DeltaItem(text=t, evidence_step=2) for t in pitfalls],
        )

    async def chat(self, model, messages, options, format, think=None):  # noqa: A002
        self.prompts.append(messages[0]["content"])
        return {"message": {"content": self._delta.model_dump_json()}}

    async def chat_with_tools(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


def _distill(
    db_path,
    store,
    llm,
    episode_id,
    verification="success",
    correction=None,
    steps=None,
    signature=_UPDATE_SIG,
    **kw,
):
    asyncio.run(
        distill_and_persist(
            trigger=signature,
            signature=signature,
            steps=steps if steps is not None else _update_steps(),
            outcome="success" if verification == "success" else "failure",
            episode_id=episode_id,
            db_path=db_path,
            knowledge_store=store,
            model="m",
            llm_client=llm,
            verification=verification,
            correction=correction,
            **kw,
        )
    )


def _entries(db_path, signature=_UPDATE_SIG):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM runbook_entries WHERE signature = ? ORDER BY created_at",
                (signature,),
            )
        ]


def _learned(db_path, signature=_UPDATE_SIG):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT id, title, approach, runbook_state, version, episode_refs"
            " FROM agent_strategies WHERE signature = ?"
            " AND runbook_state IN ('candidate', 'validated', 'discarded')",
            (signature,),
        ).fetchone()


def _insert_seed(db_path, signature, approach):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO agent_strategies (id, title, trigger_pattern, approach,"
            " runbook_state, created_at, signature)"
            " VALUES ('seed:x', 'Seed', 't', ?, 'seed', datetime('now'), ?)",
            (approach, signature),
        )
        conn.commit()


class TestDistilledDeltaSchema:
    def test_valid_construction(self):
        d = DistilledDelta(
            decision_rules=[DeltaItem(text="Rule", evidence_step=1)],
            pitfalls=[DeltaItem(text="Pitfall", evidence_step=2)],
        )
        assert d.decision_rules[0].text == "Rule"
        assert d.pitfalls[0].evidence_step == 2
        assert d.instance_facts == []

    def test_defaults_are_empty(self):
        d = DistilledDelta()
        assert d.decision_rules == [] and d.pitfalls == [] and d.instance_facts == []

    def test_item_missing_evidence_step_rejected(self):
        with pytest.raises(ValidationError):
            DeltaItem(text="Rule")  # type: ignore[call-arg]

    def test_json_round_trip(self):
        d = DistilledDelta(
            decision_rules=[DeltaItem(text="Rule", evidence_step=1)],
            instance_facts=[InstanceFact(fact="F", evidence_step=1)],
        )
        restored = DistilledDelta.model_validate_json(d.model_dump_json())
        assert restored == d

    def test_has_no_whole_runbook_fields(self):
        props = DistilledDelta.model_json_schema()["properties"]
        assert set(props) == {"decision_rules", "pitfalls", "instance_facts"}


class TestProceduralLessonSchema:
    def test_valid_construction(self):
        from utils.knowledge.runbook_distiller import ProceduralLesson

        assert ProceduralLesson(lesson="x", evidence_step=3).evidence_step == 3

    def test_missing_evidence_step_rejected(self):
        from utils.knowledge.runbook_distiller import ProceduralLesson

        with pytest.raises(ValidationError):
            ProceduralLesson(lesson="some lesson")  # type: ignore[call-arg]

    def test_json_round_trip(self):
        from utils.knowledge.runbook_distiller import ProceduralLesson

        p = ProceduralLesson(lesson="x", evidence_step=2)
        assert ProceduralLesson.model_validate_json(p.model_dump_json()) == p


class TestOverlaps:
    def test_mostly_covered_overlaps(self):
        from utils.knowledge.runbook_distiller import _overlaps

        seed = "If the device is absent from the LAN it is a physical fault."
        assert _overlaps("Device absent from LAN means physical fault", seed)

    def test_novel_tokens_do_not_overlap(self):
        from utils.knowledge.runbook_distiller import _overlaps

        seed = "If the device is absent from the LAN it is a physical fault."
        assert not _overlaps("Sonos speakers drop after router firmware upgrade", seed)

    def test_framing_words_do_not_dilute_overlap(self):
        """Replay of the Sonos session (528af01b): a seed restatement wrapped in
        "the decisive factor … the session treated as …" must still overlap."""
        from utils.knowledge.runbook_distiller import _overlaps

        seed = (
            "absent from LAN: power, Wi-Fi or physical presence. The device is "
            "off, unplugged or absent from the LAN; a local network fault, not "
            "the software integration."
        )
        text = (
            "The decisive factor was NetAlertX showing the device absent from the "
            "LAN, which the session treated as physical network fault rather than "
            "a software integration issue."
        )
        assert _overlaps(text, seed)

    def test_no_content_tokens_never_overlaps(self):
        from utils.knowledge.runbook_distiller import _overlaps

        assert not _overlaps("it is", "it is")

    def test_threshold_boundary(self):
        from utils.knowledge.runbook_distiller import _overlaps

        # 3 of 5 tokens covered = 0.6 → overlaps; 2 of 5 → does not.
        assert _overlaps("alpha bravo charlie delta echo", "alpha bravo charlie")
        assert not _overlaps("alpha bravo charlie delta echo", "alpha bravo")


class TestUnknownTools:
    KNOWN = frozenset({"get_entity_history", "read_logs", "run_ha_command"})

    def test_unregistered_verb_prefixed_name_flagged(self):
        from utils.knowledge.runbook_distiller import _unknown_tools

        text = "Call get_netalertx_presence first."
        assert _unknown_tools(text, self.KNOWN) == ["get_netalertx_presence"]

    def test_called_name_flagged_whatever_its_prefix(self):
        from utils.knowledge.runbook_distiller import _unknown_tools

        assert _unknown_tools("use probe_lan(ip)", self.KNOWN) == ["probe_lan"]

    def test_slugs_entity_ids_and_known_tools_pass(self):
        from utils.knowledge.runbook_distiller import _unknown_tools

        text = (
            "read_logs for core_mosquitto, then check sensor.noaa_tides_2 with "
            "get_entity_history."
        )
        assert _unknown_tools(text, self.KNOWN) == []

    def test_first_tool(self):
        from utils.knowledge.runbook_distiller import _first_tool

        assert _first_tool("core_mosquitto: use read_logs", self.KNOWN) == "read_logs"
        assert _first_tool("nothing here", self.KNOWN) is None

    def test_known_tool_names_covers_registries(self):
        from utils.knowledge.runbook_distiller import _known_tool_names

        names = _known_tool_names()
        assert "run_ha_command" in names and "query_knowledge" in names


class TestGateItem:
    KNOWN = frozenset(
        {"read_logs", "get_entity_history", "check_config_against_breaking_change"}
    )
    EVIDENCE = (
        "check_config_against_breaking_change({})\n"
        "configuration.yaml uses keep_days under recorder; mosquitto 7.1.3 notes"
    )

    def _gate(self, text, seed_text="", wildcard=True, evidence=EVIDENCE):
        from utils.knowledge.runbook_distiller import _gate_item

        return _gate_item(
            text,
            evidence=evidence,
            seed_text=seed_text,
            wildcard_seed=wildcard,
            known_tools=self.KNOWN,
        )

    def test_passes(self):
        assert self._gate(_RULE) is None

    def test_ungrounded(self):
        assert self._gate("Zigbee coordinator firmware lost the USB path") == (
            "ungrounded"
        )

    def test_seed_overlap(self):
        seed = "When configuration.yaml uses keep_days under recorder, rename it."
        assert self._gate(_RULE, seed_text=seed) == "seed_overlap"

    def test_version_pinned_only_for_wildcard_seed(self):
        text = "mosquitto 7.1.3 notes show configuration.yaml uses keep_days"
        assert self._gate(text, wildcard=True) == "version_pinned"
        assert self._gate(text, wildcard=False) is None

    def test_unknown_tool(self):
        # The name is in the trace (a log line mentioned it) but is no Pueo tool.
        text = "get_recorder_schema shows configuration.yaml uses keep_days"
        evidence = self.EVIDENCE + "\nlog: get_recorder_schema() deprecated"
        assert self._gate(text, evidence=evidence) == "unknown_tool"


class TestWriteEntries:
    def test_insert_then_reinforce_then_unchanged(self, tmp_path):
        from utils.knowledge.runbook_distiller import _write_entries

        db = _make_db(tmp_path)
        item = [("decision_rule", _RULE, None)]
        assert _write_entries(db, _UPDATE_SIG, "ep1", item)[0][0][0] == "inserted"
        near_dup = [("decision_rule", _RULE.replace("first", "before update"), None)]
        assert _write_entries(db, _UPDATE_SIG, "ep2", near_dup)[0][0][0] == "reinforced"
        assert _write_entries(db, _UPDATE_SIG, "ep2", item)[0][0][0] == "unchanged"
        rows = _entries(db)
        assert len(rows) == 1
        assert rows[0]["text"] == _RULE
        assert rows[0]["success_count"] == 2
        assert json.loads(rows[0]["episode_refs"]) == ["ep1", "ep2"]

    def test_kinds_do_not_merge(self, tmp_path):
        from utils.knowledge.runbook_distiller import _write_entries

        db = _make_db(tmp_path)
        _write_entries(db, _UPDATE_SIG, "ep1", [("decision_rule", _RULE, None)])
        _write_entries(db, _UPDATE_SIG, "ep1", [("pitfall", _RULE, None)])
        assert sorted(r["kind"] for r in _entries(db)) == ["decision_rule", "pitfall"]

    def test_procedure_notes_match_exactly(self, tmp_path):
        from utils.knowledge.runbook_distiller import _same_entry

        entry = {"kind": "procedure", "text": "`read_logs(x)` returned no results"}
        assert _same_entry("procedure", "`read_logs(x)`  returned no results", entry)
        assert not _same_entry("procedure", "`read_logs(y)` returned no results", entry)
        assert not _same_entry("pitfall", entry["text"], entry)


class TestRecordEntryFailures:
    def _seed_entry(self, db, created_at, kind="decision_rule", success=1):
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO runbook_entries (id, signature, kind, text, episode_refs,"
                " success_count, failure_count, created_at, updated_at)"
                " VALUES (?, ?, ?, 'r', '[]', ?, 0, ?, ?)",
                (
                    f"e-{kind}-{created_at}",
                    _UPDATE_SIG,
                    kind,
                    success,
                    created_at,
                    created_at,
                ),
            )
            conn.commit()

    def test_retires_at_failure_above_success(self, tmp_path):
        from utils.knowledge.runbook_distiller import _record_entry_failures

        db = _make_db(tmp_path)
        self._seed_entry(db, "2026-10-01T00:00:00Z")
        assert _record_entry_failures(db, _UPDATE_SIG, "2026-10-02T00:00:00Z") == 0
        assert _entries(db)[0]["failure_count"] == 1
        assert _record_entry_failures(db, _UPDATE_SIG, "2026-10-03T00:00:00Z") == 1
        assert _entries(db)[0]["retired_at"] is not None

    def test_entries_newer_than_session_and_procedure_untouched(self, tmp_path):
        from utils.knowledge.runbook_distiller import _record_entry_failures

        db = _make_db(tmp_path)
        self._seed_entry(db, "2026-10-05T00:00:00Z")
        self._seed_entry(db, "2026-10-01T00:00:00Z", kind="procedure")
        _record_entry_failures(db, _UPDATE_SIG, "2026-10-02T00:00:00Z")
        assert all(r["failure_count"] == 0 for r in _entries(db))

    def test_session_created_at(self, tmp_path):
        from utils.knowledge.runbook_distiller import _session_created_at

        db = _make_db(tmp_path)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO session_outcomes (episode_id, created_at)"
                " VALUES ('ep', '2026-10-01T00:00:00Z')"
            )
            conn.commit()
        assert _session_created_at(db, "ep") == "2026-10-01T00:00:00Z"
        assert _session_created_at(db, "missing").startswith("20")
        assert _session_created_at(str(tmp_path / "nope" / "x.db"), "ep")


class TestRenderApproach:
    def _e(self, kind, text, s=1, f=0, at="2026-10-01"):
        return {
            "kind": kind,
            "text": text,
            "success_count": s,
            "failure_count": f,
            "created_at": at,
        }

    def test_sections_order_and_ranking(self):
        from utils.knowledge.runbook_distiller import _render_approach

        out = _render_approach(
            [
                self._e("procedure", "note"),
                self._e("decision_rule", "weak", s=1),
                self._e("decision_rule", "strong", s=3),
                self._e("pitfall", "trap"),
            ]
        )
        assert out.index("## Decision rules") < out.index("## Pitfalls")
        assert out.rstrip().endswith("## Procedure notes\n- note")
        assert out.index("- strong") < out.index("- weak")

    def test_empty_sections_omitted(self):
        from utils.knowledge.runbook_distiller import _render_approach

        out = _render_approach([self._e("procedure", "note")])
        assert out == "## Procedure notes\n- note"
        assert _render_approach([]) == ""


class TestRenderLearnedRunbook:
    def test_insert_update_keeps_state_and_signature(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import (
            _write_entries,
            render_learned_runbook,
        )

        db, store = _make_db(tmp_path), FakeKnowledgeStore()
        assert render_learned_runbook(db, _UPDATE_SIG, store) is None
        _write_entries(db, _UPDATE_SIG, "ep1", [("decision_rule", _RULE, None)])
        sid = render_learned_runbook(db, _UPDATE_SIG, store)
        row = _learned(db)
        assert row[0] == sid and row[1] == "Learned: update:core:ha"
        assert row[2] == f"## Decision rules\n- {_RULE}"
        assert row[3] == "candidate" and row[4] == 1
        assert json.loads(row[5]) == ["ep1"]
        chunk = store.query("keep_days", top_k=5, collections=["strategies"])[0]
        assert chunk.metadata["signature"] == _UPDATE_SIG

        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE agent_strategies SET runbook_state='validated'")
            conn.commit()
        _write_entries(db, _UPDATE_SIG, "ep2", [("pitfall", "Other trap", None)])
        assert render_learned_runbook(db, _UPDATE_SIG, store) == sid
        row = _learned(db)
        assert row[3] == "validated" and row[4] == 2
        assert "## Pitfalls\n- Other trap" in row[2]

    def test_all_entries_retired_discards_runbook(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import (
            _write_entries,
            render_learned_runbook,
        )

        db, store = _make_db(tmp_path), FakeKnowledgeStore()
        _write_entries(db, _UPDATE_SIG, "ep1", [("decision_rule", _RULE, None)])
        render_learned_runbook(db, _UPDATE_SIG, store)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE runbook_entries SET retired_at = 'now'")
            conn.commit()
        assert render_learned_runbook(db, _UPDATE_SIG, store) is None
        assert _learned(db)[3] == "discarded"
        assert store.collection_count("strategies") == 0

    def test_chroma_failures_are_logged_not_raised(self, tmp_path):
        from utils.knowledge.runbook_distiller import (
            _write_entries,
            render_learned_runbook,
        )

        class _BrokenStore:
            def upsert(self, *a, **kw):
                raise RuntimeError("chroma down")

            def delete_ids(self, *a, **kw):
                raise RuntimeError("chroma down")

        db = _make_db(tmp_path)
        _write_entries(db, _UPDATE_SIG, "ep1", [("decision_rule", _RULE, None)])
        assert render_learned_runbook(db, _UPDATE_SIG, _BrokenStore())
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE runbook_entries SET retired_at = 'now'")
            conn.commit()
        assert render_learned_runbook(db, _UPDATE_SIG, _BrokenStore()) is None


class TestFetchSeedForSignature:
    def test_returns_signature_and_text(self, tmp_path):
        from utils.knowledge.runbook_distiller import (
            _fetch_seed_for_signature,
            _fetch_seed_text_for_signature,
        )

        db = _make_db(tmp_path)
        _insert_seed(db, "update:core:*", "Do X")
        assert _fetch_seed_for_signature(db, _UPDATE_SIG) == (
            "update:core:*",
            "# Seed\n\nDo X",
        )
        assert _fetch_seed_text_for_signature(db, _UPDATE_SIG) == "# Seed\n\nDo X"
        assert _fetch_seed_for_signature(db, "device:offline:zha") == ("", "")
        assert _fetch_seed_for_signature(db, "nocolon") == ("", "")
        assert _fetch_seed_for_signature(str(tmp_path / "a" / "b.db"), _UPDATE_SIG) == (
            "",
            "",
        )


class TestVerdictLaneEntries:
    def test_grounded_rule_becomes_entry_and_rendered(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db, store = _make_db(tmp_path), FakeKnowledgeStore()
        _distill(
            db,
            store,
            _DeltaLLM(rules=[_RULE]),
            "ep1",
            deviated_from_runbook={"rule": "tried X", "why": "Y worked"},
        )
        rows = _entries(db)
        assert [(r["kind"], r["text"], r["success_count"]) for r in rows] == [
            ("decision_rule", _RULE, 1)
        ]
        assert _learned(db)[2] == f"## Decision rules\n- {_RULE}"
        assert store.collection_count("strategies") == 1

    def test_second_session_reinforces_and_lists_known_entries(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db, store = _make_db(tmp_path), FakeKnowledgeStore()
        _distill(
            db,
            store,
            _DeltaLLM(rules=[_RULE]),
            "ep1",
            deviated_from_runbook={"rule": "tried X", "why": "Y worked"},
        )
        llm = _DeltaLLM(rules=[_RULE])
        _distill(
            db,
            store,
            llm,
            "ep2",
            deviated_from_runbook={"rule": "tried X", "why": "Y worked"},
        )
        rows = _entries(db)
        assert len(rows) == 1 and rows[0]["success_count"] == 2
        assert f"Already learned for this signature:\n- {_RULE}" in llm.prompts[0]
        row = _learned(db)
        assert row[4] == 2 and json.loads(row[5]) == ["ep1", "ep2"]

    @pytest.mark.parametrize(
        "rule, reason_seed",
        [
            ("Zigbee coordinator lost its USB path", None),  # ungrounded
            ("get_update_release_notes 2026.11 shows keep_days renamed", None),
            ("Use get_recorder_schema when configuration.yaml uses keep_days", None),
            (_RULE, "When configuration.yaml uses keep_days under recorder rename it"),
        ],
    )
    def test_gated_items_never_written(self, tmp_path, rule, reason_seed):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db = _make_db(tmp_path)
        if reason_seed:
            _insert_seed(db, "update:core:*", reason_seed)
        _distill(db, FakeKnowledgeStore(), _DeltaLLM(pitfalls=[rule]), "ep1")
        assert _entries(db) == []
        assert _learned(db) is None

    def test_procedure_notes_survive_verdict_write(self, tmp_path):
        """Regression (#2 in the 2026-10-28 review): a verdict merge erased notes."""
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import distill_procedure

        db, store = _make_db(tmp_path), FakeKnowledgeStore()
        steps = [_fail_step(1, "hassio")] + _update_steps()
        asyncio.run(
            distill_procedure(
                signature=_UPDATE_SIG,
                steps=steps,
                outcome="success",
                episode_id="ep1",
                db_path=db,
                knowledge_store=store,
            )
        )
        _distill(
            db,
            store,
            _DeltaLLM(rules=[_RULE]),
            "ep1",
            steps=steps,
            deviated_from_runbook={"rule": "tried X", "why": "Y worked"},
        )
        approach = _learned(db)[2]
        assert f"## Decision rules\n- {_RULE}" in approach
        assert "## Procedure notes\n- `get_integration_diagnostics(hassio)` failed" in (
            approach
        )

    def test_failure_with_correction_retires_shown_entry(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db, store = _make_db(tmp_path), FakeKnowledgeStore()
        _distill(
            db,
            store,
            _DeltaLLM(rules=[_RULE]),
            "ep1",
            deviated_from_runbook={"rule": "tried X", "why": "Y worked"},
        )
        for ep in ("ep2", "ep3"):
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO session_outcomes (episode_id, signature, created_at)"
                    " VALUES (?, ?, '2999-01-01T00:00:00Z')",
                    (ep, _UPDATE_SIG),
                )
                conn.commit()
            _distill(
                db, store, _DeltaLLM(), ep, verification="failure", correction="No"
            )
        rows = _entries(db)
        assert rows[0]["failure_count"] == 2 and rows[0]["retired_at"] is not None
        assert _learned(db)[3] == "discarded"

    def test_llm_failure_still_renders_after_retirement(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge import runbook_distiller as rd

        db, store = _make_db(tmp_path), FakeKnowledgeStore()
        calls = []
        monkeypatch.setattr(rd, "_record_entry_failures", lambda *a: 1)
        monkeypatch.setattr(
            rd, "render_learned_runbook", lambda *a: calls.append(a) or None
        )

        class _BrokenLLM:
            async def chat(self, **kw):
                raise RuntimeError("Ollama down")

        _distill(db, store, _BrokenLLM(), "ep1", verification="failure", correction="x")
        assert len(calls) == 1

    def test_sqlite_errors_are_logged_not_raised(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge import runbook_distiller as rd

        def _boom(*a, **kw):
            raise sqlite3.OperationalError("locked")

        db = _make_db(tmp_path)
        monkeypatch.setattr(rd, "_record_entry_failures", _boom)
        monkeypatch.setattr(rd, "_write_entries", _boom)
        monkeypatch.setattr(rd, "render_learned_runbook", _boom)
        _distill(
            db,
            FakeKnowledgeStore(),
            _DeltaLLM(rules=[_RULE]),
            "ep1",
            verification="failure",
            correction="x",
        )
        assert _entries(db) == []


class TestProcedureLane:
    SIG = "device:offline:cast"

    def _run(self, db, steps, ep="ep_x1", store=None):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.runbook_distiller import distill_procedure

        asyncio.run(
            distill_procedure(
                signature=self.SIG,
                steps=steps,
                outcome="monitoring",
                episode_id=ep,
                db_path=db,
                knowledge_store=store or FakeKnowledgeStore(),
            )
        )

    def test_extracted_notes_become_procedure_entries(self, tmp_path):
        db = _make_db(tmp_path)
        self._run(db, [_fail_step()])
        rows = _entries(db, self.SIG)
        assert [(r["kind"], r["tool"]) for r in rows] == [
            ("procedure", "get_integration_diagnostics")
        ]
        approach = _learned(db, self.SIG)[2]
        assert approach.startswith("## Procedure notes\n- `get_integration_diagnostics")
        with sqlite3.connect(db) as conn:
            quirks = conn.execute(
                "SELECT COUNT(*) FROM user_prefs WHERE key LIKE 'procedure_quirk:%'"
            ).fetchone()[0]
        assert quirks == 1

    def test_repeat_sessions_count_not_duplicate(self, tmp_path):
        db = _make_db(tmp_path)
        self._run(db, [_fail_step()], ep="a")
        self._run(db, [_fail_step()], ep="b")
        rows = _entries(db, self.SIG)
        assert len(rows) == 1 and rows[0]["success_count"] == 2

    def test_no_notes_writes_nothing_and_no_distilled_at(self, tmp_path):
        db = _make_db(tmp_path)
        _insert_session(db, "ep_x1", self.SIG)
        self._run(db, [_step("read_logs", 1, output="fine")])
        assert _entries(db, self.SIG) == []
        assert _learned(db, self.SIG) is None
        self._run(db, [_fail_step()])
        assert _distilled_at(db, "ep_x1") is None

    def test_missing_inputs_noop(self, tmp_path):
        self._run("", [_fail_step()])  # no db path → no crash

    def test_persist_failure_logged(self, tmp_path, monkeypatch):
        from utils.knowledge import runbook_distiller as rd

        def _boom(*a, **kw):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(rd, "_write_entries", _boom)
        db = _make_db(tmp_path)
        self._run(db, [_fail_step()])
        assert _learned(db, self.SIG) is None


class TestDistillAndPersistPaths:
    def test_non_success_writes_gap_not_entry(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db, store = _make_db(tmp_path), FakeKnowledgeStore()
        asyncio.run(
            distill_and_persist(
                trigger="update_poll",
                signature=_UPDATE_SIG,
                steps=_update_steps(),
                outcome="exhausted",
                episode_id="ep1",
                db_path=db,
                knowledge_store=store,
                model="m",
            )
        )
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT runbook_state FROM agent_strategies").fetchone()
        assert row[0] == "gap"
        assert store.collection_count("strategies") == 0
        assert _entries(db) == []

    def test_unrecognised_verification_skips(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db = _make_db(tmp_path)
        llm = _DeltaLLM(rules=[_RULE])
        _distill(db, FakeKnowledgeStore(), llm, "ep1", verification="maybe")
        assert llm.prompts == [] and _entries(db) == []

    def test_success_resolves_gap_and_prompt_carries_context(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        db = _make_db(tmp_path)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO agent_strategies (id, title, trigger_pattern, approach,"
                " runbook_state, created_at, signature) VALUES ('gap1', 'Gap', 't',"
                " 'x', 'gap', datetime('now'), ?)",
                (_UPDATE_SIG,),
            )
            conn.commit()
        _insert_seed(db, "update:core:*", "Seed body")
        llm = _DeltaLLM()
        _distill(
            db,
            FakeKnowledgeStore(),
            llm,
            "ep1",
            initial_context="HA core update detected",
            episode_stub={"summary": "Update applied cleanly"},
        )
        with sqlite3.connect(db) as conn:
            state = conn.execute(
                "SELECT runbook_state FROM agent_strategies WHERE id='gap1'"
            ).fetchone()[0]
        assert state == "resolved"
        assert "HA core update detected" in llm.prompts[0]
        assert "Update applied cleanly" in llm.prompts[0]
        assert "# Seed\n\nSeed body" in llm.prompts[0]


class TestLoadEntries:
    def test_missing_table_returns_empty(self, tmp_path):
        from utils.knowledge.runbook_distiller import _load_entries

        db = str(tmp_path / "empty.db")
        sqlite3.connect(db).close()
        assert _load_entries(db, _UPDATE_SIG) == []

    def test_bad_episode_refs_json_becomes_empty_list(self, tmp_path):
        from utils.knowledge.runbook_distiller import _load_entries

        db = _make_db(tmp_path)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO runbook_entries (id, signature, kind, text, episode_refs,"
                " created_at, updated_at) VALUES ('e', ?, 'pitfall', 't', 'not json',"
                " 'a', 'a')",
                (_UPDATE_SIG,),
            )
            conn.commit()
        assert _load_entries(db, _UPDATE_SIG)[0]["episode_refs"] == []


# ---------------------------------------------------------------------------
# TestFamilyScopedEntries (ADR 052)
# ---------------------------------------------------------------------------


class TestFamilyScopedEntries:
    """Tests for _family_pattern, _subject_tokens, scope rule in _write_entries,
    _promote_siblings_to_family, and _load_entries_for_session."""

    # ----- pure-function unit tests ---

    def test_family_pattern_replaces_last_segment(self):
        from utils.knowledge.runbook_distiller import _family_pattern

        assert _family_pattern("update:addon:matter_server") == "update:addon:*"
        assert _family_pattern("update:addon:mosquitto") == "update:addon:*"

    def test_family_pattern_single_segment(self):
        from utils.knowledge.runbook_distiller import _family_pattern

        assert _family_pattern("repair") == "repair:*"

    def test_family_pattern_two_segments(self):
        from utils.knowledge.runbook_distiller import _family_pattern

        assert _family_pattern("device:offline") == "device:*"

    def test_subject_tokens_from_signature_last_segment(self):
        from utils.knowledge.runbook_distiller import _subject_tokens

        tokens = _subject_tokens("update:addon:matter_server")
        assert "matter" in tokens
        assert "server" in tokens
        # stop words and short tokens excluded
        assert "the" not in tokens
        assert "ha" not in tokens  # len < 3

    def test_subject_tokens_from_subject_key(self):
        from utils.knowledge.runbook_distiller import _subject_tokens

        tokens = _subject_tokens("update:addon:mosquitto", subject_key="mqtt broker")
        assert "mosquitto" in tokens
        assert "mqtt" in tokens
        assert "broker" in tokens

    def test_subject_tokens_wildcard_excluded(self):
        from utils.knowledge.runbook_distiller import _subject_tokens

        tokens = _subject_tokens("update:addon:*")
        assert "*" not in tokens
        assert len(tokens) == 0  # wildcard + short segments produce no tokens

    # ----- scope rule in _write_entries ---

    def test_scope_rule_subject_token_stays_concrete(self, tmp_path):
        """An item whose text contains a subject token is written under the
        concrete signature, not the family pattern."""
        from utils.knowledge.runbook_distiller import _write_entries, _subject_tokens

        db = _make_db(tmp_path)
        sig = "update:addon:matter_server"
        tokens = _subject_tokens(sig)
        item = [("decision_rule", "matter_server needs a fresh token on upgrade", None)]
        results, written_sigs = _write_entries(
            db, sig, "ep1", item, subject_tokens=tokens
        )

        assert results[0][0] == "inserted"
        assert sig in written_sigs
        # Family pattern was NOT written to
        assert "update:addon:*" not in written_sigs
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT signature FROM runbook_entries WHERE retired_at IS NULL"
            ).fetchall()
        assert all(r[0] == sig for r in rows)

    def test_scope_rule_generic_item_goes_to_family(self, tmp_path):
        """An item whose text contains no subject tokens is written under the
        family-pattern signature."""
        from utils.knowledge.runbook_distiller import _write_entries, _subject_tokens

        db = _make_db(tmp_path)
        sig = "update:addon:matter_server"
        tokens = _subject_tokens(sig)
        # Generic rule — no mention of matter_server, server, matter
        item = [
            ("decision_rule", "always take a backup before upgrading any addon", None)
        ]
        results, written_sigs = _write_entries(
            db, sig, "ep1", item, subject_tokens=tokens
        )

        assert results[0][0] == "inserted"
        assert "update:addon:*" in written_sigs
        assert sig not in written_sigs
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT signature FROM runbook_entries WHERE retired_at IS NULL"
            ).fetchall()
        assert rows[0][0] == "update:addon:*"

    def test_scope_rule_no_subject_tokens_all_go_concrete(self, tmp_path):
        """When no subject_tokens are passed, all items go to the concrete signature."""
        from utils.knowledge.runbook_distiller import _write_entries

        db = _make_db(tmp_path)
        sig = "update:addon:matter_server"
        item = [
            ("decision_rule", "always take a backup before upgrading any addon", None)
        ]
        results, written_sigs = _write_entries(db, sig, "ep1", item)

        assert results[0][0] == "inserted"
        assert sig in written_sigs
        assert "update:addon:*" not in written_sigs

    # ----- sibling promotion ---

    def test_promote_siblings_merges_near_dups(self, tmp_path):
        """When two OTHER sibling signatures both have a near-duplicate entry,
        calling _promote_siblings_to_family retires both and creates a family entry.
        The scan excludes the passed signature, so we need a third sig to trigger it."""
        from utils.knowledge.runbook_distiller import (
            _promote_siblings_to_family,
            _write_entries,
        )

        db = _make_db(tmp_path)
        rule = "always take a backup before upgrading any addon"
        # mosquitto and tasmota both have the same generic rule
        _write_entries(
            db, "update:addon:mosquitto", "ep1", [("decision_rule", rule, None)]
        )
        _write_entries(
            db, "update:addon:tasmota", "ep2", [("decision_rule", rule, None)]
        )
        # Calling with matter_server — the query scans siblings != matter_server, != *
        # finds mosquitto + tasmota, detects the near-dup and merges them
        retired = _promote_siblings_to_family(db, "update:addon:matter_server")

        assert retired == 2  # both sibling entries retired
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT signature, retired_at, promoted_to FROM runbook_entries"
                " ORDER BY signature"
            ).fetchall()
        # Family entry live; both sibling entries retired with promoted_to set
        live = [r for r in rows if r[1] is None]
        retired_rows = [r for r in rows if r[1] is not None]
        assert len(live) == 1
        assert live[0][0] == "update:addon:*"
        assert len(retired_rows) == 2
        assert all(r[2] is not None for r in retired_rows)  # promoted_to set

    def test_promote_siblings_summed_counts(self, tmp_path):
        """Family-pattern entry gets summed success_count from merged sibling entries."""
        from utils.knowledge.runbook_distiller import (
            _promote_siblings_to_family,
            _write_entries,
        )

        db = _make_db(tmp_path)
        rule = "always take a backup before upgrading any addon"
        # mosquitto reinforced twice, tasmota once
        _write_entries(
            db, "update:addon:mosquitto", "ep1", [("decision_rule", rule, None)]
        )
        _write_entries(
            db, "update:addon:mosquitto", "ep2", [("decision_rule", rule, None)]
        )
        _write_entries(
            db, "update:addon:tasmota", "ep3", [("decision_rule", rule, None)]
        )
        # Trigger promotion via a third sig
        _promote_siblings_to_family(db, "update:addon:matter_server")

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT success_count, episode_refs FROM runbook_entries"
                " WHERE signature = 'update:addon:*' AND retired_at IS NULL"
            ).fetchone()
        assert row is not None
        # mosquitto had 2 successes, tasmota had 1 → 3 total
        assert row[0] == 3
        refs = json.loads(row[1])
        assert set(refs) == {"ep1", "ep2", "ep3"}

    def test_promote_siblings_no_match_returns_zero(self, tmp_path):
        """Returns 0 when no sibling pair has near-duplicate entries."""
        from utils.knowledge.runbook_distiller import (
            _promote_siblings_to_family,
            _write_entries,
        )

        db = _make_db(tmp_path)
        # Two sibling entries with completely different text — not near-dups
        _write_entries(
            db,
            "update:addon:mosquitto",
            "ep1",
            [("decision_rule", "mosquitto bridge config must be updated", None)],
        )
        _write_entries(
            db,
            "update:addon:tasmota",
            "ep2",
            [("decision_rule", "tasmota firmware version check required", None)],
        )
        retired = _promote_siblings_to_family(db, "update:addon:matter_server")
        assert retired == 0

    # ----- _load_entries_for_session retrieval ---

    def test_load_entries_for_session_includes_family(self, tmp_path):
        """_load_entries_for_session returns both exact-sig and family-pattern entries."""
        from utils.knowledge.runbook_distiller import (
            _load_entries_for_session,
            _write_entries,
        )

        db = _make_db(tmp_path)
        family_rule = "always take a backup before upgrading any addon"
        concrete_rule = "matter_server needs a fresh token on upgrade"
        _write_entries(
            db, "update:addon:*", "ep0", [("decision_rule", family_rule, None)]
        )
        _write_entries(
            db,
            "update:addon:matter_server",
            "ep1",
            [("decision_rule", concrete_rule, None)],
        )

        entries = _load_entries_for_session(db, "update:addon:matter_server")
        texts = [e["text"] for e in entries]
        assert family_rule in texts
        assert concrete_rule in texts

    def test_load_entries_for_session_new_sibling_sees_family(self, tmp_path):
        """A new sibling subject (mosquitto) sees entries written under the family
        pattern even when it has no concrete entries of its own."""
        from utils.knowledge.runbook_distiller import (
            _load_entries_for_session,
            _write_entries,
        )

        db = _make_db(tmp_path)
        family_rule = "always take a backup before upgrading any addon"
        _write_entries(
            db, "update:addon:*", "ep0", [("decision_rule", family_rule, None)]
        )

        # mosquitto has no entries of its own
        entries = _load_entries_for_session(db, "update:addon:mosquitto")
        assert len(entries) == 1
        assert entries[0]["text"] == family_rule
        assert entries[0]["signature"] == "update:addon:*"

    def test_load_entries_for_session_empty_on_missing_table(self, tmp_path):
        from utils.knowledge.runbook_distiller import _load_entries_for_session

        db = str(tmp_path / "empty.db")
        sqlite3.connect(db).close()
        assert _load_entries_for_session(db, "update:addon:matter_server") == []


# ---------------------------------------------------------------------------
# TestPueoDefect
# ---------------------------------------------------------------------------


class TestPueoDefect:
    """Tests for pueo_defect detection in extract_procedure_facts and _record_tool_quirks."""

    def _defect_step(self, n=1):
        return _step(
            "search_log",
            n,
            {"log_name": "pueo", "pattern": "error"},
            output="",
            success=False,
            error="invalid literal for int() with base 10: '2.0'",
        )

    def test_pueo_defect_excluded_from_procedure_notes(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        notes = extract_procedure_facts([self._defect_step()])
        assert notes == [], "pueo_defect errors must not appear as procedure notes"

    def test_non_defect_failure_still_captured(self):
        from utils.knowledge.runbook_distiller import extract_procedure_facts

        steps = [
            _step(
                "run_ha_command",
                1,
                {"command": "ha apps info bad_slug"},
                output="",
                success=False,
                error="exit 1: App bad_slug does not exist",
            )
        ]
        notes = extract_procedure_facts(steps)
        assert len(notes) == 1

    def test_pueo_defect_recorded_in_quirks_with_kind(self, tmp_path):
        from utils.knowledge.runbook_distiller import _record_tool_quirks

        db = _prefs_db(tmp_path)
        _record_tool_quirks(db, "host:unexpected_reboot:unknown", [self._defect_step()])
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT key, value FROM user_prefs").fetchall()
        assert len(rows) == 1
        q = json.loads(rows[0][1])
        assert q["kind"] == "pueo_defect"

    def test_quirk_detail_normalised_for_dedup(self, tmp_path):
        from utils.knowledge.runbook_distiller import _record_tool_quirks

        db = _prefs_db(tmp_path)
        step1 = _step(
            "run_ha_command",
            1,
            {"command": "ha apps info matter_server"},
            output="",
            success=False,
            error="TypeError: 'matter_server' is not a valid slug",
        )
        step2 = _step(
            "run_ha_command",
            2,
            {"command": "ha apps info mosquitto"},
            output="",
            success=False,
            error="TypeError: 'mosquitto' is not a valid slug",
        )
        _record_tool_quirks(db, "sig1", [step1])
        _record_tool_quirks(db, "sig2", [step2])
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT key, value FROM user_prefs").fetchall()
        # Both should collapse into one quirk row (same normalised detail)
        assert len(rows) == 1
        q = json.loads(rows[0][1])
        assert q["count"] == 2
        assert "<x>" in q["detail"]

    def test_normalise_quirk_detail(self):
        from utils.knowledge.runbook_distiller import _normalise_quirk_detail

        assert "'<x>'" in _normalise_quirk_detail("TypeError: 'matter_server' invalid")
        assert "N" in _normalise_quirk_detail("error at line 42")


# ---------------------------------------------------------------------------
# TestIsAlphaHyphenated
# ---------------------------------------------------------------------------


class TestIsAlphaHyphenated:
    def test_add_on_is_alpha_hyphenated(self):
        assert _is_alpha_hyphenated("add-on") is True

    def test_built_in_is_alpha_hyphenated(self):
        assert _is_alpha_hyphenated("built-in") is True

    def test_token_with_digit_is_not(self):
        assert _is_alpha_hyphenated("core-1") is False

    def test_token_with_dot_is_not(self):
        assert _is_alpha_hyphenated("add-on.x") is False

    def test_plain_word_without_hyphen_is_not(self):
        assert _is_alpha_hyphenated("addon") is False


# ---------------------------------------------------------------------------
# TestFactIsGroundedHyphenated
# ---------------------------------------------------------------------------


class TestFactIsGroundedHyphenated:
    def test_add_on_not_treated_as_distinctive(self):
        # "add-on" should not be a distinctive token, so the fact should pass
        # when the other distinctive token (core_matter_server) is present.
        evidence = "add-on name: core_matter_server slug installed ok"
        fact = "The add-on core_matter_server is installed"
        assert _fact_is_grounded(fact, evidence) is True

    def test_hyphenated_with_digit_blocks_when_missing(self):
        # "core-1" has a digit → distinctive; if absent from evidence it blocks.
        evidence = "firmware update ok"
        fact = "core-1 eeprom is blocked"
        assert _fact_is_grounded(fact, evidence) is False


# ---------------------------------------------------------------------------
# TestIsPueoMechanics
# ---------------------------------------------------------------------------


class TestIsPueoMechanics:
    def test_session_phrase_rejected(self):
        assert _is_pueo_mechanics("The session timed out") is True

    def test_autonomy_gate_rejected(self):
        assert _is_pueo_mechanics("The autonomy gate may block this") is True

    def test_auto_apply_rejected(self):
        assert _is_pueo_mechanics("auto-apply was triggered") is True

    def test_finish_tool_rejected(self):
        assert _is_pueo_mechanics("call finish_repair to end") is True

    def test_finish_tool_other_name_rejected(self):
        assert _is_pueo_mechanics("finish_update_analysis returns outcome") is True

    def test_normal_text_passes(self):
        assert _is_pueo_mechanics("The EEPROM firmware is outdated") is False

    def test_case_insensitive_phrase(self):
        assert _is_pueo_mechanics("THE AUTONOMY GATE blocks repairs") is True


# ---------------------------------------------------------------------------
# TestPitfallStepValid
# ---------------------------------------------------------------------------


class TestPitfallStepValid:
    def test_correction_always_passes(self):
        item = DeltaItem(text="pitfall text", evidence_step=1)
        steps = [_make_step("run_ha_command", 1, success=True, output="all good")]
        assert (
            _pitfall_step_valid(item, steps, correction="user said it was wrong")
            is True
        )

    def test_failed_step_passes(self):
        item = DeltaItem(text="pitfall text", evidence_step=1)
        steps = [_make_step("run_ha_command", 1, success=False, output="error text")]
        assert _pitfall_step_valid(item, steps, correction=None) is True

    def test_empty_step_passes(self):
        item = DeltaItem(text="pitfall text", evidence_step=1)
        steps = [_make_step("run_ha_command", 1, success=True, output="   ")]
        assert _pitfall_step_valid(item, steps, correction=None) is True

    def test_success_step_without_correction_fails(self):
        item = DeltaItem(text="pitfall text", evidence_step=1)
        steps = [_make_step("run_ha_command", 1, success=True, output="firmware ok")]
        assert _pitfall_step_valid(item, steps, correction=None) is False

    def test_missing_step_fails(self):
        item = DeltaItem(text="pitfall text", evidence_step=99)
        steps = [_make_step("run_ha_command", 1, success=False, output="err")]
        assert _pitfall_step_valid(item, steps, correction=None) is False


# ---------------------------------------------------------------------------
# TestIsSurpriseLogic (via distill_and_persist)
# ---------------------------------------------------------------------------


class TestIsSurpriseLogic:
    """Test that decision_rules/pitfalls are only written on a 'surprise'."""

    SIG = "repair:config_error:yaml_parse_fail"

    def _setup(self, tmp_path):
        db_path = _make_db(tmp_path)
        _insert_session(db_path, "ep1", self.SIG)
        return db_path

    def _rule_llm(self):
        """LLM that returns a decision_rule grounded in step 1 output."""

        class _RuleLLM:
            RULE = "eeprom_update_unavailable means blocked"

            async def chat(self, **kw):
                return {
                    "message": {
                        "content": DistilledDelta(
                            decision_rules=[
                                DeltaItem(
                                    text=self.RULE + " eeprom_update_unavailable",
                                    evidence_step=1,
                                )
                            ],
                            pitfalls=[],
                            instance_facts=[],
                        ).model_dump_json()
                    }
                }

        return _RuleLLM()

    def test_no_surprise_skips_rules(self, tmp_path, caplog):
        """Success + no deviated_from_runbook → rules skipped, no agent_strategies row."""
        import logging

        db_path = self._setup(tmp_path)
        steps = [
            _make_step(
                "run_ha_command",
                1,
                output="eeprom_update_unavailable blocked",
            )
        ]
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        with caplog.at_level(logging.INFO, logger="pueo"):
            asyncio.run(
                distill_and_persist(
                    trigger=self.SIG,
                    signature=self.SIG,
                    steps=steps,
                    outcome="success",
                    episode_id="ep1",
                    db_path=db_path,
                    knowledge_store=FakeKnowledgeStore(),
                    model="qwen2.5:7b",
                    llm_client=self._rule_llm(),
                    verification="success",
                    deviated_from_runbook=None,
                )
            )
        assert any(
            "distillation_rules_skipped" in r.message for r in caplog.records
        ), "Expected distillation_rules_skipped log"
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_strategies").fetchone()[0]
        assert count == 0

    def test_deviated_from_runbook_allows_rules(self, tmp_path):
        """deviated_from_runbook set → rules written even on a success."""
        db_path = self._setup(tmp_path)
        steps = [
            _make_step(
                "run_ha_command",
                1,
                output="eeprom_update_unavailable blocked",
            )
        ]
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        asyncio.run(
            distill_and_persist(
                trigger=self.SIG,
                signature=self.SIG,
                steps=steps,
                outcome="success",
                episode_id="ep1",
                db_path=db_path,
                knowledge_store=FakeKnowledgeStore(),
                model="qwen2.5:7b",
                llm_client=self._rule_llm(),
                verification="success",
                deviated_from_runbook={"rule": "tried X first", "why": "Y worked"},
            )
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM runbook_entries WHERE kind='decision_rule'"
            ).fetchone()[0]
        assert count >= 1
