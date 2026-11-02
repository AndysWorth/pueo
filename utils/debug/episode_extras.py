"""Read-only DB joins for the debug episode page ("After the session").

``load_episode_extras`` gathers everything recorded about an episode after it
ended — verification, the HITL card, runbook usage, learned entries and facts —
plus ``llm_calls`` rows as a token/latency/thinking fallback for v1 files.

Every query is read-only and failure-tolerant: a missing table or a locked DB
yields an empty section, never an exception, so the page always renders.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.core.logging import get_logger

log = get_logger(__name__)

# Card ids become file names under the HITL dir; refuse anything path-like.
_SAFE_CARD_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")

# Status marker files written next to <card_id>.json (see dashboard._status).
_CARD_STATUS_SUFFIXES = ("approved", "deferred", "rejected", "resolved")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(sql, params)
    except sqlite3.Error as exc:
        log.debug("episode_extras_query_failed", error=str(exc))
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _iso_to_epoch(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_SAFE_EPISODE_ID = re.compile(r"^[A-Za-z0-9-]+$")


def find_episode_dir(base_dir: Path, episode_id: str) -> Path | None:
    """Return the debug episode folder for *episode_id*, or None.

    Folders are named ``<trigger>_<episode_id>``; chat sessions appear as both
    ``manual_<uuid>`` and ``session_<id>``.  The newest folder that holds an
    ``episode_data.json`` wins.  Ids with path characters are rejected.
    """
    if not episode_id or not _SAFE_EPISODE_ID.match(episode_id):
        return None
    if not base_dir.is_dir():
        return None
    matches = [
        d
        for d in base_dir.glob(f"*_{episode_id}")
        if d.is_dir() and (d / "episode_data.json").is_file()
    ]
    if not matches:
        return None
    return max(matches, key=lambda d: d.stat().st_mtime)


def load_card(hitl_dir: Path | None, card_id: str | None) -> dict[str, Any] | None:
    """Return ``{card_id, subject, body, sent_at, status, resolution}`` or None."""
    if not hitl_dir or not card_id or not _SAFE_CARD_ID.match(card_id):
        return None
    card_file = hitl_dir / f"{card_id}.json"
    card: dict[str, Any] = {"card_id": card_id, "status": "PENDING"}
    try:
        data = json.loads(card_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        card["status"] = "MISSING"
        data = {}
    card["subject"] = str(data.get("subject", ""))
    card["body"] = str(data.get("body", ""))
    card["sent_at"] = data.get("sent_at")
    for suffix in _CARD_STATUS_SUFFIXES:
        marker = hitl_dir / f"{card_id}.{suffix}"
        if marker.exists():
            card["status"] = suffix.upper()
            try:
                card["resolution"] = marker.read_text(encoding="utf-8").strip()
                card["resolved_at"] = marker.stat().st_mtime
            except OSError:
                card["resolution"] = ""
            break
    return card


def load_episode_extras(
    db_path: str | Path,
    episode_id: str,
    hitl_dir: Path | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Return the post-session data for *episode_id* (read-only).

    Keys: ``session_outcome`` (dict | None), ``card`` (dict | None),
    ``runbook_usage``, ``learned_entries``, ``facts`` and ``llm_calls`` (lists).
    *started_at* (ISO) bounds the ``agent_memory`` lookup, which has no
    episode column: facts for the session's signature/subject written at or
    after the session started.
    """
    extras: dict[str, Any] = {
        "session_outcome": None,
        "card": None,
        "runbook_usage": [],
        "learned_entries": [],
        "facts": [],
        "llm_calls": [],
    }
    if not episode_id or not Path(db_path).exists():
        return extras
    uri = f"file:{Path(db_path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        log.warning("episode_extras_db_open_failed", error=str(exc))
        return extras
    try:
        outcome_rows = _rows(
            conn,
            "SELECT episode_id, signature, session_type, subject_key, "
            "conclusion_json, created_at, verification, verified_at, verified_by, "
            "correction, distilled_at, card_id FROM session_outcomes "
            "WHERE episode_id = ?",
            (episode_id,),
        )
        outcome = outcome_rows[0] if outcome_rows else None
        extras["session_outcome"] = outcome
        extras["runbook_usage"] = _rows(
            conn,
            "SELECT strategy_id, signature, outcome, created_at, verification, "
            "verified_at FROM runbook_usage WHERE episode_id = ? ORDER BY id",
            (episode_id,),
        )
        extras["learned_entries"] = _rows(
            conn,
            "SELECT id, signature, kind, text, tool, success_count, failure_count, "
            "created_at, updated_at, retired_at, promoted_to FROM runbook_entries "
            "WHERE episode_refs LIKE ? ORDER BY created_at",
            (f'%"{episode_id}"%',),
        )
        extras["llm_calls"] = _rows(
            conn,
            "SELECT ts, call_type, latency_ms, ollama_eval_ms, ollama_load_ms, "
            "input_tokens, output_tokens, thinking_text FROM llm_calls "
            "WHERE episode_id = ? ORDER BY ts, id",
            (episode_id,),
        )
        signature = outcome.get("signature") if outcome else None
        since = _iso_to_epoch(started_at) or _iso_to_epoch(
            outcome.get("created_at") if outcome else None
        )
        if signature and since is not None:
            extras["facts"] = _rows(
                conn,
                "SELECT key, content, source, ts, signature, subject_key "
                "FROM agent_memory WHERE signature = ? AND ts >= ? "
                "AND (subject_key IS NULL OR subject_key = ? OR ? IS NULL) "
                "ORDER BY ts",
                (
                    signature,
                    since,
                    (outcome or {}).get("subject_key"),
                    (outcome or {}).get("subject_key"),
                ),
            )
    finally:
        conn.close()
    if outcome:
        extras["card"] = load_card(hitl_dir, outcome.get("card_id"))
    return extras
