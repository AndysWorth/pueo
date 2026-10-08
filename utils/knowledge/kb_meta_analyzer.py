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
    kb_pr_url: Optional[str] = None
    signature: Optional[str] = None
    version: int = 1
    usage_count: int = 0
    success_rate: Optional[float] = None  # None = no usage recorded yet


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
    validated: list[RunbookRow] = field(default_factory=list)
    duplicate_pairs: list[DuplicatePair] = field(default_factory=list)
    contribution_ready: list[RunbookRow] = field(default_factory=list)
    gaps_by_signature: list[tuple[str, list[RunbookRow]]] = field(default_factory=list)


def _load_usage_stats(db_path: str) -> dict[str, tuple[int, Optional[float]]]:
    """Return {strategy_id: (usage_count, success_rate)} from runbook_usage table."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT strategy_id,"
                " COUNT(*) AS total,"
                " SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS successes"
                " FROM runbook_usage"
                " GROUP BY strategy_id"
            ).fetchall()
    except sqlite3.OperationalError:
        # Table absent on older schema versions
        return {}
    result: dict[str, tuple[int, Optional[float]]] = {}
    for row in rows:
        sid, total, successes = row[0], int(row[1]), int(row[2])
        rate: Optional[float] = (successes / total) if total > 0 else None
        result[sid] = (total, rate)
    return result


def _load_runbooks(
    db_path: str,
    usage: dict[str, tuple[int, Optional[float]]],
) -> list[RunbookRow]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, trigger_pattern, approach, runbook_state,"
            " created_at, reviewed_at, promoted_at, contributed_at,"
            " kb_pr_url,"
            " COALESCE(signature, '') AS signature,"
            " COALESCE(version, 1) AS version"
            " FROM agent_strategies"
            " WHERE runbook_state IN ('candidate', 'gap', 'validated')"
            " ORDER BY created_at DESC"
        ).fetchall()

    result: list[RunbookRow] = []
    for r in rows:
        sid = r["id"]
        u_count, u_rate = usage.get(sid, (0, None))
        result.append(
            RunbookRow(
                id=sid,
                title=r["title"],
                trigger_pattern=r["trigger_pattern"],
                approach=r["approach"],
                runbook_state=r["runbook_state"],
                created_at=r["created_at"],
                reviewed_at=r["reviewed_at"],
                promoted_at=r["promoted_at"],
                contributed_at=r["contributed_at"],
                kb_pr_url=r["kb_pr_url"],
                signature=r["signature"] or None,
                version=int(r["version"]),
                usage_count=u_count,
                success_rate=u_rate,
            )
        )
    return result


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


def _group_gaps_by_signature(
    gaps: list[RunbookRow],
) -> list[tuple[str, list[RunbookRow]]]:
    """Return gaps grouped by signature, ungrouped last."""
    grouped: dict[str, list[RunbookRow]] = {}
    for gap in gaps:
        key = gap.signature or "(no signature)"
        grouped.setdefault(key, []).append(gap)
    # Put "(no signature)" last
    no_sig = grouped.pop("(no signature)", [])
    items = sorted(grouped.items())
    if no_sig:
        items.append(("(no signature)", no_sig))
    return items


def analyze_local_runbooks(
    db_path: str,
    knowledge_store: Optional["FakeKnowledgeStore"] = None,
) -> MetaAnalysisReport:
    """Read candidate/gap/validated runbooks and return a structured analysis report."""
    usage = _load_usage_stats(db_path)
    runbooks = _load_runbooks(db_path, usage)
    candidates = [r for r in runbooks if r.runbook_state == "candidate"]
    gaps = [r for r in runbooks if r.runbook_state == "gap"]
    validated = [r for r in runbooks if r.runbook_state == "validated"]
    duplicate_pairs = _find_duplicates(runbooks, knowledge_store)
    # Only validated/seed runbooks are eligible for contribution
    contribution_ready = [r for r in validated if r.contributed_at is None]
    gaps_by_signature = _group_gaps_by_signature(gaps)
    return MetaAnalysisReport(
        candidates=candidates,
        gaps=gaps,
        validated=validated,
        duplicate_pairs=duplicate_pairs,
        contribution_ready=contribution_ready,
        gaps_by_signature=gaps_by_signature,
    )
