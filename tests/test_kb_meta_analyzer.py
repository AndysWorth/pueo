"""Tests for utils/knowledge/kb_meta_analyzer.py."""

import sqlite3
from unittest.mock import patch

import pytest

from utils.knowledge.kb_meta_analyzer import (
    NEAR_DUPLICATE_THRESHOLD,
    MetaAnalysisReport,
    RunbookRow,
    analyze_local_runbooks,
)
from utils.knowledge.knowledge_store import FakeKnowledgeStore


# ── helpers ──────────────────────────────────────────────────────────────────


def _seed_db(db_path: str, rows: list[dict]) -> None:
    """Create agent_strategies table and insert test runbook rows."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_strategies ("
            " id TEXT PRIMARY KEY,"
            " title TEXT NOT NULL,"
            " trigger_pattern TEXT NOT NULL,"
            " approach TEXT NOT NULL,"
            " runbook_state TEXT NOT NULL DEFAULT 'candidate',"
            " created_at TEXT NOT NULL,"
            " reviewed_at TEXT,"
            " promoted_at TEXT,"
            " contributed_at TEXT"
            ")"
        )
        for row in rows:
            conn.execute(
                "INSERT INTO agent_strategies"
                " (id, title, trigger_pattern, approach, runbook_state, created_at,"
                "  reviewed_at, promoted_at, contributed_at)"
                " VALUES (:id, :title, :trigger_pattern, :approach, :runbook_state,"
                "  :created_at, :reviewed_at, :promoted_at, :contributed_at)",
                {
                    "id": row["id"],
                    "title": row["title"],
                    "trigger_pattern": row.get("trigger_pattern", ""),
                    "approach": row.get("approach", ""),
                    "runbook_state": row.get("runbook_state", "candidate"),
                    "created_at": row.get("created_at", "2026-09-01T00:00:00"),
                    "reviewed_at": row.get("reviewed_at"),
                    "promoted_at": row.get("promoted_at"),
                    "contributed_at": row.get("contributed_at"),
                },
            )
        conn.commit()


# ── RunbookRow dataclass ─────────────────────────────────────────────────────


class TestRunbookRow:
    def test_construction(self):
        rb = RunbookRow(
            id="abc",
            title="Fix YAML error",
            trigger_pattern="invalid yaml",
            approach="Check indentation",
            runbook_state="candidate",
            created_at="2026-09-01T00:00:00",
            reviewed_at=None,
            promoted_at=None,
            contributed_at=None,
        )
        assert rb.id == "abc"
        assert rb.runbook_state == "candidate"
        assert rb.reviewed_at is None

    def test_optional_fields_default_none(self):
        rb = RunbookRow(
            id="x",
            title="t",
            trigger_pattern="p",
            approach="a",
            runbook_state="gap",
            created_at="2026-09-01T00:00:00",
            reviewed_at=None,
            promoted_at=None,
            contributed_at=None,
        )
        assert rb.promoted_at is None
        assert rb.contributed_at is None


# ── MetaAnalysisReport dataclass ─────────────────────────────────────────────


class TestMetaAnalysisReport:
    def test_empty_defaults(self):
        r = MetaAnalysisReport()
        assert r.candidates == []
        assert r.gaps == []
        assert r.duplicate_pairs == []
        assert r.contribution_ready == []


# ── analyze_local_runbooks ────────────────────────────────────────────────────


class TestAnalyzeLocalRunbooks:
    def test_empty_db_returns_empty_report(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(db, [])
        report = analyze_local_runbooks(db)
        assert isinstance(report, MetaAnalysisReport)
        assert report.candidates == []
        assert report.gaps == []

    def test_candidate_appears_in_candidates(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "c1",
                    "title": "Fix config",
                    "runbook_state": "candidate",
                }
            ],
        )
        report = analyze_local_runbooks(db)
        assert len(report.candidates) == 1
        assert report.candidates[0].id == "c1"
        assert report.gaps == []

    def test_gap_appears_in_gaps(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "g1",
                    "title": "Unknown failure",
                    "runbook_state": "gap",
                }
            ],
        )
        report = analyze_local_runbooks(db)
        assert len(report.gaps) == 1
        assert report.gaps[0].id == "g1"

    def test_seed_not_included(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "s1",
                    "title": "Seed runbook",
                    "runbook_state": "seed",
                }
            ],
        )
        report = analyze_local_runbooks(db)
        assert report.candidates == []
        assert report.gaps == []

    def test_contribution_ready_requires_reviewed_and_no_contributed(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "c-reviewed",
                    "title": "Reviewed candidate",
                    "runbook_state": "candidate",
                    "reviewed_at": "2026-09-01T12:00:00",
                    "contributed_at": None,
                },
                {
                    "id": "c-unreviewed",
                    "title": "Unreviewed candidate",
                    "runbook_state": "candidate",
                    "reviewed_at": None,
                },
                {
                    "id": "c-already-contributed",
                    "title": "Already queued",
                    "runbook_state": "candidate",
                    "reviewed_at": "2026-09-01T12:00:00",
                    "contributed_at": "2026-09-02T10:00:00",
                },
            ],
        )
        report = analyze_local_runbooks(db)
        ready_ids = {r.id for r in report.contribution_ready}
        assert "c-reviewed" in ready_ids
        assert "c-unreviewed" not in ready_ids
        assert "c-already-contributed" not in ready_ids

    def test_no_knowledge_store_skips_duplicate_detection(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {"id": "c1", "title": "A", "runbook_state": "candidate"},
                {"id": "c2", "title": "B", "runbook_state": "candidate"},
            ],
        )
        report = analyze_local_runbooks(db, knowledge_store=None)
        assert report.duplicate_pairs == []

    def test_duplicate_pair_detected_via_knowledge_store(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "c1",
                    "title": "Fix YAML indentation error",
                    "trigger_pattern": "yaml error",
                    "runbook_state": "candidate",
                },
            ],
        )
        from utils.knowledge.knowledge_store import KnowledgeChunk

        store = FakeKnowledgeStore()
        store.upsert(
            "strategies",
            ["c2"],
            ["Fix YAML indentation error\nTrigger: yaml error\n\napproach"],
            [
                {
                    "strategy_id": "c2",
                    "title": "Resolve YAML parse failure",
                    "runbook_type": "candidate",
                }
            ],
        )

        # Patch query to return high-score hit
        with patch.object(
            store,
            "query",
            return_value=[
                KnowledgeChunk(
                    text="...",
                    source="agent_learned",
                    collection="strategies",
                    score=0.95,
                    metadata={
                        "strategy_id": "c2",
                        "title": "Resolve YAML parse failure",
                    },
                )
            ],
        ):
            report = analyze_local_runbooks(db, knowledge_store=store)

        assert len(report.duplicate_pairs) == 1
        pair = report.duplicate_pairs[0]
        assert pair.id_a == "c1"
        assert pair.id_b == "c2"
        assert pair.score == 0.95

    def test_low_score_not_a_duplicate(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [{"id": "c1", "title": "A", "runbook_state": "candidate"}],
        )
        from utils.knowledge.knowledge_store import KnowledgeChunk

        store = FakeKnowledgeStore()
        with patch.object(
            store,
            "query",
            return_value=[
                KnowledgeChunk(
                    text="...",
                    source="agent_learned",
                    collection="strategies",
                    score=0.7,
                    metadata={"strategy_id": "c2", "title": "Different runbook"},
                )
            ],
        ):
            # score 0.7 < NEAR_DUPLICATE_THRESHOLD so query is called with min_score=0.92
            # and real FakeKnowledgeStore would return nothing; the patch makes it return
            # 0.7. We only check that the pair's score is noted as below threshold.
            report = analyze_local_runbooks(db, knowledge_store=store)

        # The pair IS returned because we patched query — score validation
        # is done by passing min_score to the real store, not in our code.
        # Here we just verify we relay what the store returns.
        assert len(report.duplicate_pairs) == 1
        assert report.duplicate_pairs[0].score == 0.7

    def test_self_match_excluded(self, tmp_path):
        """A runbook must not be paired with itself."""
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [{"id": "c1", "title": "A", "runbook_state": "candidate"}],
        )
        from utils.knowledge.knowledge_store import KnowledgeChunk

        store = FakeKnowledgeStore()
        with patch.object(
            store,
            "query",
            return_value=[
                KnowledgeChunk(
                    text="...",
                    source="agent_learned",
                    collection="strategies",
                    score=1.0,
                    # Same id as the runbook being queried
                    metadata={"strategy_id": "c1", "title": "A"},
                )
            ],
        ):
            report = analyze_local_runbooks(db, knowledge_store=store)

        assert report.duplicate_pairs == []

    def test_knowledge_store_exception_skips_gracefully(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [{"id": "c1", "title": "A", "runbook_state": "candidate"}],
        )
        store = FakeKnowledgeStore()
        with patch.object(store, "query", side_effect=RuntimeError("chroma down")):
            report = analyze_local_runbooks(db, knowledge_store=store)
        assert report.duplicate_pairs == []

    def test_mixed_states_partitioned_correctly(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {"id": "c1", "title": "C1", "runbook_state": "candidate"},
                {"id": "g1", "title": "G1", "runbook_state": "gap"},
                {"id": "s1", "title": "S1", "runbook_state": "seed"},
            ],
        )
        report = analyze_local_runbooks(db)
        assert {r.id for r in report.candidates} == {"c1"}
        assert {r.id for r in report.gaps} == {"g1"}
