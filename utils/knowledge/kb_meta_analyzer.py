"""Local runbook meta-analysis: near-duplicate detection and contribution readiness."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from utils.knowledge.knowledge_store import FakeKnowledgeStore

NEAR_DUPLICATE_THRESHOLD = 0.92


@dataclass
class RunbookRow:
    id: str
    title: str
    trigger_pattern: str
    approach: str
    runbook_state: str
    created_at: str
    reviewed_at: Optional[str]
    promoted_at: Optional[str]
    contributed_at: Optional[str]


@dataclass
class DuplicatePair:
    id_a: str
    title_a: str
    id_b: str
    title_b: str
    score: float


@dataclass
class MetaAnalysisReport:
    candidates: list[RunbookRow] = field(default_factory=list)
    gaps: list[RunbookRow] = field(default_factory=list)
    duplicate_pairs: list[DuplicatePair] = field(default_factory=list)
    contribution_ready: list[RunbookRow] = field(default_factory=list)


def _load_runbooks(db_path: str) -> list[RunbookRow]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, trigger_pattern, approach, runbook_state,"
            " created_at, reviewed_at, promoted_at, contributed_at"
            " FROM agent_strategies"
            " WHERE runbook_state IN ('candidate', 'gap')"
            " ORDER BY created_at DESC"
        ).fetchall()
    return [
        RunbookRow(
            id=r["id"],
            title=r["title"],
            trigger_pattern=r["trigger_pattern"],
            approach=r["approach"],
            runbook_state=r["runbook_state"],
            created_at=r["created_at"],
            reviewed_at=r["reviewed_at"],
            promoted_at=r["promoted_at"],
            contributed_at=r["contributed_at"],
        )
        for r in rows
    ]


def _find_duplicates(
    runbooks: list[RunbookRow],
    knowledge_store: Optional["FakeKnowledgeStore"],
) -> list[DuplicatePair]:
    if knowledge_store is None or not runbooks:
        return []

    pairs: list[DuplicatePair] = []
    seen: set[frozenset[str]] = set()

    for rb in runbooks:
        query_text = f"{rb.title}\n{rb.trigger_pattern}"
        try:
            results = knowledge_store.query(
                query_text,
                top_k=3,
                collections=["strategies"],
                min_score=NEAR_DUPLICATE_THRESHOLD,
            )
        except Exception:  # nosec B112
            continue
        for chunk in results:
            other_id = chunk.metadata.get("strategy_id", "")
            if not other_id or other_id == rb.id:
                continue
            pair_key = frozenset([rb.id, other_id])
            if pair_key in seen:
                continue
            seen.add(pair_key)
            other_title = chunk.metadata.get("title", other_id)
            pairs.append(
                DuplicatePair(
                    id_a=rb.id,
                    title_a=rb.title,
                    id_b=other_id,
                    title_b=other_title,
                    score=chunk.score,
                )
            )
    return pairs


def analyze_local_runbooks(
    db_path: str,
    knowledge_store: Optional["FakeKnowledgeStore"] = None,
) -> MetaAnalysisReport:
    """Read candidate/gap runbooks and return a structured analysis report."""
    runbooks = _load_runbooks(db_path)
    candidates = [r for r in runbooks if r.runbook_state == "candidate"]
    gaps = [r for r in runbooks if r.runbook_state == "gap"]
    duplicate_pairs = _find_duplicates(runbooks, knowledge_store)
    contribution_ready = [
        r for r in candidates if r.reviewed_at is not None and r.contributed_at is None
    ]
    return MetaAnalysisReport(
        candidates=candidates,
        gaps=gaps,
        duplicate_pairs=duplicate_pairs,
        contribution_ready=contribution_ready,
    )
