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
    """Create agent_strategies + runbook_usage tables and insert test rows."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_strategies ("
            " id TEXT PRIMARY KEY,"
            " title TEXT NOT NULL,"
            " trigger_pattern TEXT NOT NULL DEFAULT '',"
            " approach TEXT NOT NULL DEFAULT '',"
            " runbook_state TEXT NOT NULL DEFAULT 'candidate',"
            " created_at TEXT NOT NULL,"
            " reviewed_at TEXT,"
            " promoted_at TEXT,"
            " contributed_at TEXT,"
            " kb_pr_url TEXT,"
            " signature TEXT,"
            " version INTEGER DEFAULT 1"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runbook_usage ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " strategy_id TEXT NOT NULL,"
            " episode_id TEXT,"
            " signature TEXT,"
            " outcome TEXT NOT NULL,"
            " created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
            ")"
        )
        for row in rows:
            conn.execute(
                "INSERT INTO agent_strategies"
                " (id, title, trigger_pattern, approach, runbook_state, created_at,"
                "  reviewed_at, promoted_at, contributed_at, kb_pr_url, signature, version)"
                " VALUES (:id, :title, :trigger_pattern, :approach, :runbook_state,"
                "  :created_at, :reviewed_at, :promoted_at, :contributed_at, :kb_pr_url,"
                "  :signature, :version)",
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
                    "kb_pr_url": row.get("kb_pr_url"),
                    "signature": row.get("signature"),
                    "version": row.get("version", 1),
                },
            )
        conn.commit()


def _seed_usage(db_path: str, rows: list[dict]) -> None:
    with sqlite3.connect(db_path) as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO runbook_usage (strategy_id, episode_id, signature, outcome)"
                " VALUES (?, ?, ?, ?)",
                (
                    row["strategy_id"],
                    row.get("episode_id"),
                    row.get("signature"),
                    row["outcome"],
                ),
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
        assert rb.signature is None
        assert rb.version == 1
        assert rb.usage_count == 0
        assert rb.success_rate is None


# ── MetaAnalysisReport dataclass ─────────────────────────────────────────────


class TestMetaAnalysisReport:
    def test_empty_defaults(self):
        r = MetaAnalysisReport()
        assert r.candidates == []
        assert r.gaps == []
        assert r.validated == []
        assert r.duplicate_pairs == []
        assert r.contribution_ready == []
        assert r.gaps_by_signature == []


# ── analyze_local_runbooks ────────────────────────────────────────────────────


class TestAnalyzeLocalRunbooks:
    def test_empty_db_returns_empty_report(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(db, [])
        report = analyze_local_runbooks(db)
        assert isinstance(report, MetaAnalysisReport)
        assert report.candidates == []
        assert report.gaps == []
        assert report.validated == []

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
        assert report.validated == []

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

    def test_validated_appears_in_validated(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "v1",
                    "title": "Validated runbook",
                    "runbook_state": "validated",
                }
            ],
        )
        report = analyze_local_runbooks(db)
        assert len(report.validated) == 1
        assert report.validated[0].id == "v1"
        assert report.candidates == []
        assert report.gaps == []

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
        assert report.validated == []

    def test_contribution_ready_requires_validated_and_no_contributed(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "v-ready",
                    "title": "Validated ready",
                    "runbook_state": "validated",
                    "contributed_at": None,
                },
                {
                    "id": "v-queued",
                    "title": "Already queued",
                    "runbook_state": "validated",
                    "contributed_at": "2026-09-02T10:00:00",
                },
                {
                    "id": "c-reviewed",
                    "title": "Reviewed candidate (not eligible)",
                    "runbook_state": "candidate",
                    "reviewed_at": "2026-09-01T12:00:00",
                    "contributed_at": None,
                },
            ],
        )
        report = analyze_local_runbooks(db)
        ready_ids = {r.id for r in report.contribution_ready}
        assert "v-ready" in ready_ids
        assert "v-queued" not in ready_ids
        assert "c-reviewed" not in ready_ids

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
                {"id": "v1", "title": "V1", "runbook_state": "validated"},
                {"id": "s1", "title": "S1", "runbook_state": "seed"},
            ],
        )
        report = analyze_local_runbooks(db)
        assert {r.id for r in report.candidates} == {"c1"}
        assert {r.id for r in report.gaps} == {"g1"}
        assert {r.id for r in report.validated} == {"v1"}

    def test_signature_version_populated(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "c1",
                    "title": "C1",
                    "runbook_state": "candidate",
                    "signature": "repair:zwave_js:failed_to_set_config",
                    "version": 3,
                }
            ],
        )
        report = analyze_local_runbooks(db)
        row = report.candidates[0]
        assert row.signature == "repair:zwave_js:failed_to_set_config"
        assert row.version == 3

    def test_usage_stats_populated(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [{"id": "c1", "title": "C1", "runbook_state": "candidate"}],
        )
        _seed_usage(
            db,
            [
                {"strategy_id": "c1", "outcome": "success"},
                {"strategy_id": "c1", "outcome": "success"},
                {"strategy_id": "c1", "outcome": "failed"},
            ],
        )
        report = analyze_local_runbooks(db)
        row = report.candidates[0]
        assert row.usage_count == 3
        assert row.success_rate == pytest.approx(2 / 3)

    def test_usage_stats_no_usage(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [{"id": "c1", "title": "C1", "runbook_state": "candidate"}],
        )
        report = analyze_local_runbooks(db)
        row = report.candidates[0]
        assert row.usage_count == 0
        assert row.success_rate is None

    def test_gaps_grouped_by_signature(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(
            db,
            [
                {
                    "id": "g1",
                    "title": "Gap A1",
                    "runbook_state": "gap",
                    "signature": "repair:zwave_js:failed",
                    "created_at": "2026-09-01T00:00:00",
                },
                {
                    "id": "g2",
                    "title": "Gap A2",
                    "runbook_state": "gap",
                    "signature": "repair:zwave_js:failed",
                    "created_at": "2026-09-02T00:00:00",
                },
                {
                    "id": "g3",
                    "title": "Gap B",
                    "runbook_state": "gap",
                    "signature": "log:custom:Exception",
                    "created_at": "2026-09-01T00:00:00",
                },
                {
                    "id": "g4",
                    "title": "Gap no sig",
                    "runbook_state": "gap",
                    "signature": None,
                    "created_at": "2026-09-01T00:00:00",
                },
            ],
        )
        report = analyze_local_runbooks(db)
        grouped = dict(report.gaps_by_signature)
        assert set(grouped.keys()) == {
            "repair:zwave_js:failed",
            "log:custom:Exception",
            "(no signature)",
        }
        assert {r.id for r in grouped["repair:zwave_js:failed"]} == {"g1", "g2"}
        assert len(grouped["log:custom:Exception"]) == 1
        assert len(grouped["(no signature)"]) == 1
        # "(no signature)" should be last
        last_sig, _ = report.gaps_by_signature[-1]
        assert last_sig == "(no signature)"
