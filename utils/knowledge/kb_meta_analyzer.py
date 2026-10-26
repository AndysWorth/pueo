"""Local runbook meta-analysis: near-duplicate detection and contribution readiness."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from utils.knowledge.knowledge_store import FakeKnowledgeStore

# Shared with the distiller so signature matching and note parsing agree.
from utils.knowledge.runbook_distiller import (
    _QUIRK_PREFIX,
    _sig_specificity,
    _split_procedure_notes,
)

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
    signature_stats: list["SignatureStats"] = field(default_factory=list)
    unmatched_seeds: list["RunbookRow"] = field(default_factory=list)
    tool_quirks: list["ToolQuirk"] = field(default_factory=list)


@dataclass
class SignatureStats:
    """Per-signature learning-health statistics."""

    signature: str
    sessions_30d: int = 0
    verified_success: int = 0
    verified_failure: int = 0
    pending: int = 0
    unverified: int = 0  # swept after 48 h with no verification
    last_session_at: Optional[str] = None
    matched_seed: Optional[str] = None
    learned_state: Optional[str] = None
    procedure_notes: int = 0
    instance_facts: int = 0
    last_used_at: Optional[str] = None


@dataclass
class ToolQuirk:
    """A tool failure or empty result the procedural extractor keeps seeing."""

    tool: str
    kind: str  # "failed" | "empty"
    detail: str
    example: str
    count: int
    signatures: list[str] = field(default_factory=list)
    last_seen: Optional[str] = None


_TOP_QUIRKS = 10


def _load_tool_quirks(db_path: str, limit: int = _TOP_QUIRKS) -> list[ToolQuirk]:
    """Return the most frequent recorded tool quirks (sessions seen, then breadth)."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT value FROM user_prefs WHERE key LIKE ?",
                (_QUIRK_PREFIX + "%",),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    quirks: list[ToolQuirk] = []
    for (value,) in rows:
        try:
            d = json.loads(value)
            quirks.append(
                ToolQuirk(
                    tool=str(d["tool"]),
                    kind=str(d.get("kind", "failed")),
                    detail=str(d.get("detail", "")),
                    example=str(d.get("example", "")),
                    count=int(d.get("count", 0)),
                    signatures=list(d.get("signatures", [])),
                    last_seen=d.get("last_seen"),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    quirks.sort(key=lambda q: (-q.count, -len(q.signatures), q.tool))
    return quirks[:limit]


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


def _load_signature_stats(
    db_path: str,
) -> tuple[list[SignatureStats], list[RunbookRow]]:
    """Load per-signature learning-health statistics from the session DB.

    Returns ``(stats, unmatched_seeds)`` where *stats* is sorted by
    ``sessions_30d`` desc then signature, and *unmatched_seeds* lists seed
    patterns that never matched any session_outcomes row.
    """
    # ── 1. session_outcomes ──────────────────────────────────────────────
    sig_data: dict[str, dict] = {}  # signature -> accumulators
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT signature,"
                " COUNT(*) AS total,"
                " SUM(CASE WHEN verification='success' THEN 1 ELSE 0 END) AS v_success,"
                " SUM(CASE WHEN verification='failure' THEN 1 ELSE 0 END) AS v_failure,"
                " SUM(CASE WHEN verification='pending' THEN 1 ELSE 0 END) AS v_pending,"
                " SUM(CASE WHEN verification='unverified' THEN 1 ELSE 0 END) AS v_unverified,"
                " MAX(created_at) AS last_session,"
                " SUM(CASE WHEN created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 days')"
                "         THEN 1 ELSE 0 END) AS sessions_30d"
                " FROM session_outcomes"
                " WHERE signature IS NOT NULL"
                "   AND (verification IS NULL OR verification != 'superseded')"
                " GROUP BY signature"
            ).fetchall()
        for r in rows:
            sig = r[0]
            sig_data[sig] = {
                "sessions_30d": int(r[7]),
                "verified_success": int(r[2]),
                "verified_failure": int(r[3]),
                "pending": int(r[4]),
                "unverified": int(r[5]),
                "last_session_at": r[6],
            }
    except sqlite3.OperationalError:
        # Table absent on older schema — fall through with empty stats.
        pass

    # ── 2. agent_strategies — seeds, learned runbooks, unmatched seeds ───
    # One pass: each stored (possibly wildcard) signature is matched against
    # every concrete session signature, so all per-signature results are
    # keyed by the session signature.
    seeds_by_sig: dict[str, list[tuple[str, int]]] = {}  # sig -> [(title, spec)]
    # sig -> (state, title, specificity, procedure-note bullet count)
    learned_by_sig: dict[str, tuple[str, str, int, int]] = {}
    unmatched: list[RunbookRow] = []

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, signature, runbook_state, approach"
                " FROM agent_strategies"
                " WHERE signature IS NOT NULL AND runbook_state IN ('seed','validated','candidate')"
            ).fetchall()
        for r in rows:
            stored_sig = r["signature"]
            title = r["title"] or stored_sig
            state = r["runbook_state"]
            is_seed = state == "seed"
            note_count = 0
            if not is_seed:
                _, notes = _split_procedure_notes(r["approach"] or "")
                note_count = sum(
                    1 for line in notes.splitlines() if line.strip().startswith("- ")
                )
            found = False
            for sig_key in sig_data:
                matches, spec = _sig_specificity(stored_sig, sig_key)
                if not matches:
                    continue
                found = True
                if is_seed:
                    seeds_by_sig.setdefault(sig_key, []).append((title, spec))
                    continue
                # Prefer validated over candidate, then highest specificity.
                old = learned_by_sig.get(sig_key)
                if (
                    old is None
                    or (state == "validated" and old[0] != "validated")
                    or (state == old[0] and spec > old[2])
                ):
                    learned_by_sig[sig_key] = (state, title, spec, note_count)
            if is_seed and not found:
                unmatched.append(
                    RunbookRow(
                        id=r["id"],
                        title=title,
                        trigger_pattern="",
                        approach="",
                        runbook_state="seed",
                        created_at="",
                        reviewed_at=None,
                        promoted_at=None,
                        contributed_at=None,
                        signature=stored_sig,
                    )
                )
    except sqlite3.OperationalError:
        pass

    # ── 3. instance_facts from agent_memory ──────────────────────────────
    fact_counts: dict[str, int] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT signature, COUNT(*) AS cnt"
                " FROM agent_memory"
                " WHERE signature IS NOT NULL"
                " GROUP BY signature"
            ).fetchall()
        for r in rows:
            fact_counts[r[0]] = int(r[1])
    except sqlite3.OperationalError:
        pass

    # ── 4. last_used_at from runbook_usage ───────────────────────────────
    used_at: dict[str, Optional[str]] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT signature, MAX(created_at) AS last_used"
                " FROM runbook_usage"
                " WHERE signature IS NOT NULL"
                " GROUP BY signature"
            ).fetchall()
        for r in rows:
            used_at[r[0]] = r[1]
    except sqlite3.OperationalError:
        pass

    # ── 5. Build SignatureStats rows ─────────────────────────────────────
    stats: list[SignatureStats] = []
    for sig, d in sig_data.items():
        # Matched seed: highest specificity seed wins.
        matched_seed: Optional[str] = None
        if seeds_by_sig.get(sig):
            matched_seed = max(seeds_by_sig[sig], key=lambda x: x[1])[0]

        learned_state: Optional[str] = None
        procedure_notes = 0
        if sig in learned_by_sig:
            state, title, _, procedure_notes = learned_by_sig[sig]
            learned_state = f"{state}: {title}"

        stats.append(
            SignatureStats(
                signature=sig,
                sessions_30d=d["sessions_30d"],
                verified_success=d["verified_success"],
                verified_failure=d["verified_failure"],
                pending=d["pending"],
                unverified=d["unverified"],
                last_session_at=d["last_session_at"],
                matched_seed=matched_seed,
                learned_state=learned_state,
                procedure_notes=procedure_notes,
                instance_facts=fact_counts.get(sig, 0),
                last_used_at=used_at.get(sig),
            )
        )

    # Sort: sessions_30d desc, then signature asc.
    stats.sort(key=lambda s: (-s.sessions_30d, s.signature))

    return stats, unmatched


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
    sig_stats, unmatched_seeds = _load_signature_stats(db_path)
    return MetaAnalysisReport(
        candidates=candidates,
        gaps=gaps,
        validated=validated,
        duplicate_pairs=duplicate_pairs,
        contribution_ready=contribution_ready,
        gaps_by_signature=gaps_by_signature,
        signature_stats=sig_stats,
        unmatched_seeds=unmatched_seeds,
        tool_quirks=_load_tool_quirks(db_path),
    )
