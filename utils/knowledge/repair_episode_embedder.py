"""Embed repair episodes into the repair_history ChromaDB collection (ADR 031)."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_log = logging.getLogger("repair_episode_embedder")


def embed_repair_episodes(store: "KnowledgeStoreClientProtocol", db_path: str) -> int:
    """Embed unembedded repair episodes into the repair_history collection.

    Reads episodes where embedded_at IS NULL, creates one text chunk per
    episode using format_episode_for_embedding(), upserts them into the
    repair_history ChromaDB collection, and marks embedded_at.

    Returns the count of newly embedded episodes.  Best-effort — any step
    that fails is logged and 0 is returned so the rag-refresh caller can
    continue without raising.
    """
    from utils.repair.repair_episode import format_episode_for_embedding

    try:
        episodes = _load_unembedded_episodes(db_path)
    except Exception as exc:
        _log.warning("repair_episode_load_failed", extra={"error": str(exc)})
        return 0

    if not episodes:
        return 0

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    # S3: skip duplicate conclusions — same (signature, subject_key, text hash).
    # Still mark embedded_at so they are never re-queued on the next RAG refresh.
    # Both the first (embedded) and subsequent (skipped) ids go into embedded_ids.
    _seen_conclusion_keys: set[tuple[str, str, str]] = set()
    all_ids: list[str] = []
    skipped = 0

    for ep, correction, sig, subj in episodes:
        text = format_episode_for_embedding(ep, correction=correction)
        ep_id = f"episode_{ep.id}"
        all_ids.append(ep.id)
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        dedup_key = (sig or ep.trigger, subj or "", text_hash)
        if dedup_key in _seen_conclusion_keys:
            skipped += 1
            continue
        _seen_conclusion_keys.add(dedup_key)
        ids.append(ep_id)
        documents.append(text)
        metadatas.append(
            {
                "source": f"repair_episode:{ep.id}",
                "outcome": "success" if ep.verification_result else "failed",
                "model_used": ep.model_used,
                "trigger": ep.trigger,
                "timestamp": ep.timestamp,
            }
        )

    if ids:
        try:
            store.upsert("repair_history", ids, documents, metadatas)
        except Exception as exc:
            _log.warning("repair_episode_upsert_failed", extra={"error": str(exc)})
            return 0

    now = time.time()
    try:
        _mark_episodes_embedded(db_path, all_ids, now)
    except Exception as exc:
        _log.warning("repair_episode_mark_embedded_failed", extra={"error": str(exc)})

    embedded = len(ids)
    _log.info(
        "repair_episodes_embedded",
        extra={"count": embedded, "skipped_duplicates": skipped},
    )
    return embedded


def _load_unembedded_episodes(db_path: str) -> list:
    """Load verified repair episodes that have not yet been embedded.

    Only embeds episodes that have a session_outcomes row with verification in
    ('success', 'failure') — i.e. confirmed by the system or a user action.
    Pre-S2 episodes that have no session_outcomes row at all are embedded as
    before (left-join NULL rows pass through).

    Returns a list of (RepairEpisode, correction: str | None, signature: str, subject_key: str)
    tuples.  signature and subject_key come from session_outcomes (empty for legacy episodes).
    """
    import json

    from utils.repair.repair_episode import RepairEpisode
    from utils.agent.tool_registry import ToolCall

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # V39 (2026-10-16, epoch 1760572800) introduced session_outcomes.
        # Episodes with no row that predate V39 are legacy and pass through.
        # Episodes with no row that postdate V39 are awaiting verification and
        # must not be embedded — they would embed unverified conclusions.
        _V39_EPOCH = 1760572800.0  # 2026-10-16T00:00:00 UTC
        rows = conn.execute(
            "SELECT r.*, s.correction, s.signature AS so_signature,"
            "       s.subject_key AS so_subject_key"
            " FROM repair_episodes r"
            " LEFT JOIN session_outcomes s ON r.id = s.episode_id"
            " WHERE r.embedded_at IS NULL"
            "   AND (s.verification IN ('success', 'failure')"
            "     OR (s.episode_id IS NULL AND r.timestamp < ?))",
            (_V39_EPOCH,),
        ).fetchall()

    episodes = []
    for row in rows:
        keys = row.keys()
        raw_seq = json.loads(row["tool_sequence"])
        tool_sequence = []
        tool_result_summaries = []
        for tc in raw_seq:
            tc = dict(tc)
            summary = tc.pop("result_summary", "")
            tool_sequence.append(ToolCall(**tc))
            tool_result_summaries.append(summary)
        ep = RepairEpisode(
            id=row["id"],
            timestamp=row["timestamp"],
            trigger=row["trigger"],
            symptoms=json.loads(row["symptoms"]),
            tool_sequence=tool_sequence,
            tool_result_summaries=tool_result_summaries,
            hypothesis_chain=json.loads(row["hypothesis_chain"]),
            fix_applied=row["fix_applied"],
            verification_result=bool(row["verification_result"]),
            model_used=row["model_used"],
            escalated=bool(row["escalated"]),
            duration_seconds=row["duration_seconds"],
            submitted_at=row["submitted_at"] if "submitted_at" in keys else None,
            pr_url=row["pr_url"] if "pr_url" in keys else None,
            initial_context=(
                row["initial_context"] if "initial_context" in keys else None
            ),
            activity_type=(row["activity_type"] if "activity_type" in keys else None),
        )
        correction = row["correction"] if "correction" in keys else None
        sig = row["so_signature"] if "so_signature" in keys else None
        subj = row["so_subject_key"] if "so_subject_key" in keys else None
        episodes.append((ep, correction, sig or "", subj or ""))
    return episodes


def _mark_episodes_embedded(
    db_path: str, episode_ids: list[str], timestamp: float
) -> None:
    """Update embedded_at for a list of episode IDs."""
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "UPDATE repair_episodes SET embedded_at = ? WHERE id = ?",
            [(timestamp, ep_id) for ep_id in episode_ids],
        )
