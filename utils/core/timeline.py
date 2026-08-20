"""Timeline event persistence and SSE publication."""

import json
import sqlite3
import time
from typing import Any, Optional

from config import DB_PATH


def write_timeline_event(
    level: str,
    source: str,
    message: str,
    detail: Optional[dict[str, Any]] = None,
) -> int:
    """Write a timeline event to SQLite and publish to the SSE bus. Returns the event id."""
    ts = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO timeline_events (ts, level, source, message, detail_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, level, source, message, json.dumps(detail) if detail else None),
        )
        event_id: int = cur.lastrowid or 0
    try:
        from utils.supervisor import publish_event

        publish_event(
            {
                "event_type": "timeline",
                "id": event_id,
                "ts": ts,
                "level": level,
                "source": source,
                "message": message,
                "detail_url": f"/timeline/{event_id}",
            }
        )
    except Exception:  # nosec B110 — SSE publish is best-effort
        pass
    return event_id


def load_timeline_events(
    limit: int = 50,
    offset: int = 0,
    level_filter: str = "",
    source_filter: str = "",
) -> list[dict[str, Any]]:
    """Return timeline_events from SQLite, newest-first, with optional filters."""
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if level_filter:
            clauses.append("level = ?")
            params.append(level_filter)
        if source_filter:
            clauses.append("source = ?")
            params.append(source_filter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, ts, level, source, message, detail_json"
                " FROM timeline_events "
                + where  # nosec B608 — where is built from a fixed column allowlist, not user input
                + " ORDER BY ts DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
    except Exception:
        return []
    result = []
    for row in rows:
        record = dict(row)
        raw = record.pop("detail_json", None)
        record["detail"] = json.loads(raw) if raw else {}
        result.append(record)
    return result


def get_timeline_event(event_id: int) -> Optional[dict[str, Any]]:
    """Return a single timeline event by id, or None if not found."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, ts, level, source, message, detail_json"
                " FROM timeline_events WHERE id = ?",
                (event_id,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    record = dict(row)
    raw = record.pop("detail_json", None)
    record["detail"] = json.loads(raw) if raw else {}
    return record


def count_timeline_events(level_filter: str = "", source_filter: str = "") -> int:
    """Return the total count of timeline_events matching optional filters."""
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if level_filter:
            clauses.append("level = ?")
            params.append(level_filter)
        if source_filter:
            clauses.append("source = ?")
            params.append(source_filter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM timeline_events "
                + where,  # nosec B608 — where is built from a fixed column allowlist, not user input
                params,
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
