"""Embed repair episodes into the repair_history ChromaDB collection (ADR 031)."""

from __future__ import annotations

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

    for ep in episodes:
        text = format_episode_for_embedding(ep)
        ids.append(f"episode_{ep.id}")
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

    try:
        store.upsert("repair_history", ids, documents, metadatas)
    except Exception as exc:
        _log.warning("repair_episode_upsert_failed", extra={"error": str(exc)})
        return 0

    now = time.time()
    embedded_ids = [ep.id for ep in episodes]
    try:
        _mark_episodes_embedded(db_path, embedded_ids, now)
    except Exception as exc:
        _log.warning("repair_episode_mark_embedded_failed", extra={"error": str(exc)})

    _log.info("repair_episodes_embedded", extra={"count": len(episodes)})
    return len(episodes)


def _load_unembedded_episodes(db_path: str) -> list:
    """Load repair episodes that have not yet been embedded (embedded_at IS NULL)."""
    import json

    from utils.repair.repair_episode import RepairEpisode
    from utils.agent.tool_registry import ToolCall

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM repair_episodes WHERE embedded_at IS NULL"
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
        episodes.append(
            RepairEpisode(
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
                activity_type=(
                    row["activity_type"] if "activity_type" in keys else None
                ),
            )
        )
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
