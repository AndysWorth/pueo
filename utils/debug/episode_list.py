"""Rows for the ``/episodes`` list, built from the debug episode folders.

``repair_episodes`` only holds successful repairs, so failed, stuck and
exhausted sessions never appeared in the old list.  Every AgentLoop session
with ``debug_level >= 1`` writes ``<trigger>_<episode_id>/episode_data.json``,
so the folders are the complete record.  ``scan_episode_dirs`` summarises each
file (v1 and v2), ``join_session_outcomes`` adds verification from the DB, and
``filter_rows`` applies the list filters.

Parsing is cached per file (path, mtime, size): the largest files are a few MB
and the list is reloaded far more often than episodes are written.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.core.logging import get_logger

log = get_logger(__name__)

# Outcome filter options always offered, in this order; any other outcome
# seen on disk is appended after them.
STANDARD_OUTCOMES = (
    "success",
    "needs_action",
    "monitoring",
    "awaiting_approval",
    "failed",
    "stuck",
    "exhausted",
)


@dataclass(frozen=True)
class EpisodeRow:
    episode_id: str
    folder: str
    trigger: str
    session_type: str
    signature: str
    outcome: str
    started_at: str  # ISO-ish display string ("" when unknown)
    sort_ts: float
    duration_seconds: float | None
    model: str
    llm_calls: int
    tool_calls: int
    schema_version: int
    agent_episode_id: str = ""
    verification: str = ""
    verified_by: str = ""
    card_id: str = ""


_cache: dict[str, tuple[int, int, EpisodeRow | None]] = {}


def _parse_ts(value: Any) -> float | None:
    """Epoch for a v2 ISO ``...Z`` stamp or a v1 ``YYYY-MM-DD HH:MM:SS``."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # v1 stamps were written in local time
    return dt.timestamp()


def summarize_episode(folder: Path, data: dict[str, Any]) -> EpisodeRow:
    """Pure: one list row from a parsed ``episode_data.json``."""
    suffix = folder.name.rsplit("_", 1)[-1]
    trigger = str(data.get("trigger") or folder.name.rsplit("_", 1)[0])
    started = data.get("started_at") or data.get("timestamp") or ""
    ts = _parse_ts(started)
    duration = data.get("duration_seconds")
    return EpisodeRow(
        episode_id=suffix,
        folder=folder.name,
        trigger=trigger,
        session_type=str(data.get("session_type") or trigger),
        signature=str(data.get("signature") or ""),
        outcome=str(data.get("outcome") or "unknown"),
        started_at=str(started),
        sort_ts=ts if ts is not None else 0.0,
        duration_seconds=float(duration) if duration is not None else None,
        model=str(data.get("model") or ""),
        llm_calls=len(data.get("llm_calls") or []),
        tool_calls=len(data.get("tool_calls") or []),
        schema_version=int(data.get("schema_version") or 1),
        agent_episode_id=str(data.get("agent_episode_id") or ""),
    )


def _load_row(folder: Path) -> EpisodeRow | None:
    path = folder / "episode_data.json"
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _cache.get(key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    row: EpisodeRow | None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        row = summarize_episode(folder, data) if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        log.debug("episode_list_parse_failed", folder=folder.name, error=str(exc))
        row = None
    _cache[key] = (st.st_mtime_ns, st.st_size, row)
    return row


def scan_episode_dirs(base_dir: Path) -> list[EpisodeRow]:
    """All episode rows under *base_dir*, newest first.

    A chat turn is written twice: by AgentLoop as ``manual_<uuid>`` and by the
    dashboard as ``session_<id>`` (which names the uuid in
    ``agent_episode_id``).  The ``manual_`` copy is dropped when its
    ``session_`` twin exists, since the latter carries the chat metadata.
    """
    if not base_dir.is_dir():
        return []
    rows = [r for r in (_load_row(d) for d in base_dir.iterdir() if d.is_dir()) if r]
    twins = {r.agent_episode_id for r in rows if r.agent_episode_id}
    rows = [r for r in rows if r.agent_episode_id or r.episode_id not in twins]
    rows.sort(key=lambda r: r.sort_ts, reverse=True)
    return rows


def join_session_outcomes(
    rows: list[EpisodeRow], db_path: str | Path
) -> list[EpisodeRow]:
    """Add verification, verified_by, card_id (and a missing signature) from
    ``session_outcomes`` (read-only; any DB error leaves rows unchanged)."""
    if not rows or not Path(db_path).exists():
        return rows
    ids = {r.agent_episode_id or r.episode_id for r in rows}
    found: dict[str, tuple[Any, ...]] = {}
    try:
        conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        log.warning("episode_list_db_open_failed", error=str(exc))
        return rows
    try:
        placeholders = ",".join("?" * len(ids))
        for rec in conn.execute(
            "SELECT episode_id, verification, verified_by, card_id, signature "
            f"FROM session_outcomes WHERE episode_id IN ({placeholders})",  # nosec B608 — placeholders only
            tuple(ids),
        ):
            found[str(rec[0])] = rec
    except sqlite3.Error as exc:
        log.debug("episode_list_outcomes_query_failed", error=str(exc))
    finally:
        conn.close()
    out = []
    for r in rows:
        rec = found.get(r.agent_episode_id or r.episode_id)
        if rec is None:
            out.append(r)
            continue
        out.append(
            replace(
                r,
                verification=str(rec[1] or ""),
                verified_by=str(rec[2] or ""),
                card_id=str(rec[3] or ""),
                signature=r.signature or str(rec[4] or ""),
            )
        )
    return out


def filter_rows(
    rows: list[EpisodeRow], outcome: str = "", session_type: str = ""
) -> list[EpisodeRow]:
    """Pure: keep rows matching *outcome* and *session_type* (empty = any).

    ``outcome="non_success"`` keeps everything that is not ``success``.
    """
    if outcome == "non_success":
        rows = [r for r in rows if r.outcome != "success"]
    elif outcome:
        rows = [r for r in rows if r.outcome == outcome]
    if session_type:
        rows = [r for r in rows if r.session_type == session_type]
    return rows


def outcome_options(rows: list[EpisodeRow]) -> list[str]:
    """Standard outcomes first, then any other outcome present in *rows*."""
    extra = sorted({r.outcome for r in rows} - set(STANDARD_OUTCOMES))
    return list(STANDARD_OUTCOMES) + extra


def format_started(row: EpisodeRow) -> str:
    """``YYYY-MM-DD HH:MM UTC`` for display, or the raw value."""
    if not row.sort_ts:
        return row.started_at or "—"
    return datetime.fromtimestamp(row.sort_ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
