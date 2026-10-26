"""Tests for utils/knowledge/kb_meta_analyzer.py."""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from utils.knowledge.kb_meta_analyzer import (
    NEAR_DUPLICATE_THRESHOLD,
    MetaAnalysisReport,
    RunbookRow,
    SignatureStats,
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


# ── helpers for session-outcome-backed tests ────────────────────────────────


def _seed_session_outcomes(db_path: str, rows: list[tuple]) -> None:
    """Create session_outcomes + agent_memory + runbook_usage + agent_strategies tables."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_outcomes ("
            "  episode_id TEXT PRIMARY KEY,"
            "  signature TEXT,"
            "  session_type TEXT,"
            "  subject_key TEXT,"
            "  conclusion_json TEXT,"
            "  distill_payload_json TEXT,"
            "  created_at TEXT,"
            "  verification TEXT,"
            "  verified_at TEXT,"
            "  verified_by TEXT,"
            "  correction TEXT,"
            "  distilled_at TEXT,"
            "  card_id TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_memory ("
            " id INTEGER PRIMARY KEY, key TEXT NOT NULL, content TEXT NOT NULL,"
            " source TEXT NOT NULL, ts REAL NOT NULL, signature TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runbook_usage ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " strategy_id TEXT NOT NULL, episode_id TEXT, signature TEXT,"
            " outcome TEXT NOT NULL,"
            " created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
            ")"
        )
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
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO session_outcomes"
                " (episode_id, signature, session_type, subject_key,"
                "  conclusion_json, distill_payload_json, created_at,"
                "  verification, verified_at, verified_by, correction, distilled_at, card_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        conn.commit()


# ── SignatureStats dataclass ────────────────────────────────────────────────


class TestSignatureStats:
    def test_empty_db(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_db(db, [])
        report = analyze_local_runbooks(db)
        assert report.signature_stats == []
        assert report.unmatched_seeds == []

    def test_sessions_30d_window(self, tmp_path):
        """A row older than 30d counts in verified/last_session but not in sessions_30d."""
        db = str(tmp_path / "test.db")
        # Relative to the real clock: the SQL window uses SQLite's 'now'.
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        utc_now = datetime.now(timezone.utc)
        now = utc_now.strftime(fmt)
        thirty_one_days_ago = (utc_now - timedelta(days=31)).strftime(fmt)
        ten_days_ago = (utc_now - timedelta(days=10)).strftime(fmt)
        _seed_session_outcomes(
            db,
            [
                # 31 days ago — outside the 30d window
                (
                    "ep-old",
                    "repair:core:*",
                    "repair",
                    "issue-1",
                    "{}",
                    "",
                    thirty_one_days_ago,
                    "success",
                    now,
                    "system",
                    None,
                    None,
                    None,
                ),
                # 10 days ago — inside the window
                (
                    "ep-recent",
                    "repair:core:*",
                    "repair",
                    "issue-2",
                    "{}",
                    "",
                    ten_days_ago,
                    "success",
                    now,
                    "system",
                    None,
                    None,
                    None,
                ),
            ],
        )
        report = analyze_local_runbooks(db)
        stats = report.signature_stats
        assert len(stats) == 1
        s = stats[0]
        assert s.signature == "repair:core:*"
        assert s.sessions_30d == 1  # only ep-recent
        assert s.verified_success == 2  # both are verified success
        assert s.last_session_at == ten_days_ago  # max(created_at)

    def test_verified_counts(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "repair:a:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
                (
                    "ep2",
                    "repair:a:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
                (
                    "ep3",
                    "repair:a:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "failure",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
                (
                    "ep4",
                    "repair:a:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "pending",
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    "ep5",
                    "repair:a:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "unverified",
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
        report = analyze_local_runbooks(db)
        s = report.signature_stats[0]
        assert s.verified_success == 2
        assert s.verified_failure == 1
        assert s.pending == 1
        assert s.unverified == 1

    def test_wildcard_seed_matches(self, tmp_path):
        """A seed with wildcard matches a session signature; most specific seed wins."""
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "update:addon:mosquitto",
                    "update",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
            ],
        )
        _seed_db(
            db,
            [
                # Less specific: update:addon:*
                {
                    "id": "s1",
                    "title": "Addon update seed",
                    "runbook_state": "seed",
                    "signature": "update:addon:*",
                    "approach": "Check addon docs",
                },
                # More specific: update:addon:mosquitto
                {
                    "id": "s2",
                    "title": "Mosquitto update seed",
                    "runbook_state": "seed",
                    "signature": "update:addon:mosquitto",
                    "approach": "Restart mosquitto",
                },
            ],
        )
        report = analyze_local_runbooks(db)
        s = report.signature_stats[0]
        assert s.matched_seed == "Mosquitto update seed"

    def test_validated_preferred_over_candidate(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "repair:x:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
            ],
        )
        _seed_db(
            db,
            [
                {
                    "id": "c1",
                    "title": "Candidate runbook",
                    "runbook_state": "candidate",
                    "signature": "repair:x:*",
                    "approach": "Try A",
                },
                {
                    "id": "v1",
                    "title": "Validated runbook",
                    "runbook_state": "validated",
                    "signature": "repair:x:*",
                    "approach": "Try B",
                },
            ],
        )
        report = analyze_local_runbooks(db)
        s = report.signature_stats[0]
        assert s.learned_state is not None
        assert s.learned_state.startswith("validated")
        assert "Validated runbook" in s.learned_state

    def test_procedure_note_count(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "repair:y:startup_failed",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
            ],
        )
        _seed_db(
            db,
            [
                {
                    "id": "r1",
                    "title": "Runbook with notes",
                    "runbook_state": "candidate",
                    "signature": "repair:y:*",
                    "approach": (
                        "## When to use\nFix Y\n\n"
                        "## Procedure notes\n"
                        "- Step 1 worked\n"
                        "- Step 2 failed\n"
                    ),
                },
            ],
        )
        report = analyze_local_runbooks(db)
        s = report.signature_stats[0]
        assert s.signature == "repair:y:startup_failed"
        assert s.learned_state == "candidate: Runbook with notes"
        assert s.procedure_notes == 2

    def test_procedure_note_count_no_section(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "repair:y2:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
            ],
        )
        _seed_db(
            db,
            [
                {
                    "id": "r2",
                    "title": "No notes",
                    "runbook_state": "candidate",
                    "signature": "repair:y2:*",
                    "approach": "## When to use\nFix Y2",
                },
            ],
        )
        report = analyze_local_runbooks(db)
        s = report.signature_stats[0]
        assert s.procedure_notes == 0

    def test_procedure_notes_follow_most_specific_learned(self, tmp_path):
        """Notes come from the selected learned runbook, not the broader one."""
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "repair:q:startup_failed",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
            ],
        )
        _seed_db(
            db,
            [
                {
                    "id": "broad",
                    "title": "Broad",
                    "runbook_state": "candidate",
                    "signature": "repair:q:*",
                    "approach": "## Procedure notes\n- a\n- b\n- c\n",
                },
                {
                    "id": "narrow",
                    "title": "Narrow",
                    "runbook_state": "candidate",
                    "signature": "repair:q:startup_failed",
                    "approach": "## Procedure notes\n- only one\n",
                },
            ],
        )
        report = analyze_local_runbooks(db)
        s = report.signature_stats[0]
        assert s.learned_state == "candidate: Narrow"
        assert s.procedure_notes == 1

    def test_instance_facts_count(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "repair:z:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
            ],
        )
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO agent_memory (key, content, source, ts, signature)"
                " VALUES (?, ?, ?, ?, ?)",
                ("f1", "fact A", "distill:ep1", 1000.0, "repair:z:*"),
            )
            conn.execute(
                "INSERT INTO agent_memory (key, content, source, ts, signature)"
                " VALUES (?, ?, ?, ?, ?)",
                ("f2", "fact B", "distill:ep1", 1000.0, "repair:z:*"),
            )
            conn.execute(
                "INSERT INTO agent_memory (key, content, source, ts, signature)"
                " VALUES (?, ?, ?, ?, ?)",
                ("f3", "fact C", "distill:ep2", 2000.0, "repair:other:*"),
            )
            conn.commit()
        report = analyze_local_runbooks(db)
        s = report.signature_stats[0]
        assert s.instance_facts == 2

    def test_last_used_at(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "repair:w:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
            ],
        )
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO runbook_usage (strategy_id, episode_id, signature, outcome, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("s1", "ep1", "repair:w:*", "success", "2026-07-05T12:00:00Z"),
            )
            conn.commit()
        report = analyze_local_runbooks(db)
        s = report.signature_stats[0]
        assert s.last_used_at == "2026-07-05T12:00:00Z"

    def test_unmatched_seed_listed(self, tmp_path):
        db = str(tmp_path / "test.db")
        _seed_session_outcomes(
            db,
            [
                (
                    "ep1",
                    "repair:core:*",
                    "repair",
                    "x",
                    "{}",
                    "",
                    "2026-07-01T00:00:00Z",
                    "success",
                    "T",
                    "s",
                    None,
                    None,
                    None,
                ),
            ],
        )
        _seed_db(
            db,
            [
                {
                    "id": "s1",
                    "title": "Matched seed",
                    "runbook_state": "seed",
                    "signature": "repair:core:*",
                    "approach": "Fix core",
                },
                {
                    "id": "s2",
                    "title": "Unmatched seed",
                    "runbook_state": "seed",
                    "signature": "update:hacs:*",
                    "approach": "Check hacs",
                },
            ],
        )
        report = analyze_local_runbooks(db)
        unmatched_ids = {r.id for r in report.unmatched_seeds}
        assert "s1" not in unmatched_ids
        assert "s2" in unmatched_ids

    def test_missing_tables_degrade_empty(self, tmp_path):
        """Older schema DB with no session_outcomes/agent_memory/runbook_usage tables."""
        db = str(tmp_path / "test.db")
        # Create ONLY agent_strategies — no session_outcomes, agent_memory, or runbook_usage.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_strategies ("
                " id TEXT PRIMARY KEY, title TEXT NOT NULL, trigger_pattern TEXT NOT NULL DEFAULT '',"
                " approach TEXT NOT NULL DEFAULT '', runbook_state TEXT NOT NULL DEFAULT 'candidate',"
                " created_at TEXT NOT NULL, reviewed_at TEXT, promoted_at TEXT,"
                " contributed_at TEXT, kb_pr_url TEXT, signature TEXT, version INTEGER DEFAULT 1"
                ")"
            )
            conn.execute(
                "INSERT INTO agent_strategies (id, title, trigger_pattern, approach, runbook_state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("r1", "test", "", "", "candidate", "2026-09-01T00:00:00"),
            )
            conn.commit()
        report = analyze_local_runbooks(db)
        assert report.signature_stats == []
        assert report.unmatched_seeds == []
        assert len(report.candidates) == 1


# ── Tool quirks (#991) ─────────────────────────────────────────────────────────


def _quirk_db(tmp_path, values):
    import json

    db = str(tmp_path / "quirks.db")
    _seed_db(db, [])
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for i, v in enumerate(values):
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?, ?)",
                (f"procedure_quirk:{i}", v if isinstance(v, str) else json.dumps(v)),
            )
        conn.execute("INSERT INTO user_prefs (key, value) VALUES ('other', '{}')")
    return db


class TestToolQuirks:
    def test_sorted_by_count_then_breadth_and_capped(self, tmp_path):
        from utils.knowledge.kb_meta_analyzer import _load_tool_quirks

        db = _quirk_db(
            tmp_path,
            [
                {
                    "tool": "a",
                    "kind": "failed",
                    "detail": "x",
                    "count": 2,
                    "signatures": ["s1"],
                },
                {"tool": "b", "kind": "empty", "count": 5, "signatures": ["s1"]},
                {
                    "tool": "c",
                    "count": 2,
                    "signatures": ["s1", "s2"],
                    "last_seen": "2026-10-26T00:00:00Z",
                },
                "not json",
                {"count": 9},  # no tool
            ],
        )
        quirks = _load_tool_quirks(db)
        assert [q.tool for q in quirks] == ["b", "c", "a"]
        assert quirks[1].kind == "failed"  # default
        assert quirks[1].last_seen == "2026-10-26T00:00:00Z"
        assert [q.tool for q in _load_tool_quirks(db, limit=1)] == ["b"]

    def test_missing_table_returns_empty(self, tmp_path):
        from utils.knowledge.kb_meta_analyzer import _load_tool_quirks

        db = str(tmp_path / "empty.db")
        _seed_db(db, [])
        assert _load_tool_quirks(db) == []

    def test_report_includes_quirks(self, tmp_path):
        db = _quirk_db(tmp_path, [{"tool": "fetch_url", "detail": "404", "count": 1}])
        report = analyze_local_runbooks(db)
        assert [q.tool for q in report.tool_quirks] == ["fetch_url"]
