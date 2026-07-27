#!/usr/bin/env python3
"""HA persistent notification triage: schema, rule-based classification, and history tracking."""

import sqlite3
import time
from typing import Literal, Optional

from pydantic import BaseModel

from config import DB_PATH
from utils.logging import get_logger

log = get_logger("ha_notification_manager")


class NotificationAnalysis(BaseModel):
    notification_id: str
    category: Literal["security", "update", "config_error", "integration", "other"]
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    original_title: Optional[str]
    original_message: str
    human_explanation: str
    enriched_context: dict
    recommended_action: str
    requires_hitl: bool


_CLASSIFICATION_MAP: dict[str, tuple[str, str]] = {
    "http_login": ("security", "HIGH"),
    "invalid_config": ("config_error", "HIGH"),
}


def classify_notification(notification_id: str) -> tuple[str, str]:
    """Return (category, severity) for a notification_id using rule-based lookup."""
    return _CLASSIFICATION_MAP.get(notification_id, ("other", "MEDIUM"))


def record_notification_seen(
    notification_id: str,
    category: str,
    severity: str,
    db_path: str = DB_PATH,
) -> bool:
    """Insert notification into history if new; update last_seen_at if existing.

    Returns True if newly seen (first time), False if already in history.
    """
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        existing = conn.execute(
            "SELECT notification_id FROM notification_history WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE notification_history SET last_seen_at = ? WHERE notification_id = ?",
                (now, notification_id),
            )
            return False
        conn.execute(
            """
            INSERT INTO notification_history
                (notification_id, first_seen_at, last_seen_at, category, severity)
            VALUES (?, ?, ?, ?, ?)
            """,
            (notification_id, now, now, category, severity),
        )
    return True


def mark_notification_hitl_sent(
    notification_id: str,
    db_path: str = DB_PATH,
) -> None:
    """Record that a HITL card was dispatched for this notification."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE notification_history SET hitl_sent_at = ? WHERE notification_id = ?",
            (time.time(), notification_id),
        )


def mark_notification_dismissed(
    notification_id: str,
    dismissed_by: str = "user",
    db_path: str = DB_PATH,
) -> None:
    """Record dismissal after the user clicks Dismiss in the HITL card."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE notification_history SET dismissed_at = ?, dismissed_by = ? WHERE notification_id = ?",
            (time.time(), dismissed_by, notification_id),
        )


def get_notification_history(
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return all rows from notification_history ordered by first_seen_at descending."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM notification_history ORDER BY first_seen_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_pending_notifications(
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return notifications not yet dismissed."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM notification_history WHERE dismissed_at IS NULL ORDER BY first_seen_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]
