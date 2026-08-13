"""DB-backed HITL card suppression: rejection memory, cooldown, and Known Issues."""

import sqlite3
import time
from typing import Any


def should_send_card(
    conn: sqlite3.Connection,
    card_key: str,
    *,
    skip_pending_check: bool = False,
) -> bool:
    """Return False if the card is actively suppressed or already pending user action.

    skip_pending_check=True bypasses the "in-queue" guard — use only for callers that
    have their own periodic retry logic (e.g. ResourcePoller cooldown retries) and need
    to re-send while respecting explicit user suppression (rejection / known issues).
    """
    row = conn.execute(
        "SELECT known_issue, next_allowed_at, resolved_at, last_action, send_count"
        " FROM hitl_suppression WHERE card_key = ?",
        (card_key,),
    ).fetchone()
    if row is None:
        return True
    known_issue, next_allowed_at, resolved_at, last_action, send_count = row
    if resolved_at is not None:
        return True  # Condition previously cleared; this is a fresh occurrence.
    if known_issue:
        return False
    if last_action in ("rejected", "deferred"):
        # Allow re-send only once the cooldown/defer window has elapsed.
        return next_allowed_at is None or next_allowed_at <= time.time()
    if skip_pending_check:
        return True  # Caller owns retry cadence; only explicit suppression blocks here.
    # Card is in the HITL queue (sent, no action yet) — do not re-fire.
    return send_count == 0


def mark_card_sent(
    conn: sqlite3.Connection, card_key: str, card_type: str, description: str
) -> None:
    """Upsert: insert on first send, update last_sent_at + send_count on repeat."""
    now = time.time()
    conn.execute(
        """
        INSERT INTO hitl_suppression (card_key, card_type, description, first_sent_at, last_sent_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(card_key) DO UPDATE SET
            last_sent_at = excluded.last_sent_at,
            send_count = send_count + 1,
            resolved_at = NULL,
            last_action = NULL,
            next_allowed_at = NULL
        """,
        (card_key, card_type, description, now, now),
    )
    conn.commit()


def mark_card_rejected(
    conn: sqlite3.Connection, card_key: str, cooldown_hours: float
) -> None:
    """Extend cooldown on rejection; doubles with each successive rejection."""
    now = time.time()
    row = conn.execute(
        "SELECT rejection_count FROM hitl_suppression WHERE card_key = ?",
        (card_key,),
    ).fetchone()
    rejection_count = (row[0] if row else 0) + 1
    # Cooldown doubles: 1st → 1×, 2nd → 2×, 3rd → 3×, etc.
    next_allowed_at = now + (rejection_count * cooldown_hours * 3600)
    conn.execute(
        """
        INSERT INTO hitl_suppression
            (card_key, card_type, description, first_sent_at, last_sent_at,
             last_action, last_action_at, rejection_count, next_allowed_at)
        VALUES (?, '', '', ?, ?, 'rejected', ?, ?, ?)
        ON CONFLICT(card_key) DO UPDATE SET
            last_action = 'rejected',
            last_action_at = excluded.last_action_at,
            rejection_count = excluded.rejection_count,
            next_allowed_at = excluded.next_allowed_at
        """,
        (card_key, now, now, now, rejection_count, next_allowed_at),
    )
    conn.commit()


def mark_card_acknowledged(
    conn: sqlite3.Connection, card_key: str, note: str = ""
) -> None:
    """Mark as a Known Issue — no further cards until condition resolves."""
    now = time.time()
    conn.execute(
        """
        INSERT INTO hitl_suppression
            (card_key, card_type, description, first_sent_at, last_sent_at,
             last_action, last_action_at, known_issue, known_issue_note)
        VALUES (?, '', '', ?, ?, 'acknowledged', ?, 1, ?)
        ON CONFLICT(card_key) DO UPDATE SET
            last_action = 'acknowledged',
            last_action_at = excluded.last_action_at,
            known_issue = 1,
            known_issue_note = excluded.known_issue_note,
            next_allowed_at = NULL,
            resolved_at = NULL
        """,
        (card_key, now, now, now, note),
    )
    conn.commit()


def mark_card_deferred(
    conn: sqlite3.Connection, card_key: str, hours: float = 24.0
) -> None:
    """Suppress the card until hours from now."""
    now = time.time()
    next_allowed_at = now + hours * 3600
    conn.execute(
        """
        INSERT INTO hitl_suppression
            (card_key, card_type, description, first_sent_at, last_sent_at,
             last_action, last_action_at, next_allowed_at)
        VALUES (?, '', '', ?, ?, 'deferred', ?, ?)
        ON CONFLICT(card_key) DO UPDATE SET
            last_action = 'deferred',
            last_action_at = excluded.last_action_at,
            next_allowed_at = excluded.next_allowed_at
        """,
        (card_key, now, now, now, next_allowed_at),
    )
    conn.commit()


def mark_card_resolved(conn: sqlite3.Connection, card_key: str) -> None:
    """Mark condition as resolved; next occurrence starts fresh."""
    now = time.time()
    conn.execute(
        "UPDATE hitl_suppression SET resolved_at = ?, known_issue = 0 WHERE card_key = ?",
        (now, card_key),
    )
    conn.commit()


def get_rejection_count(conn: sqlite3.Connection, card_key: str) -> int:
    """Return how many times this card has been rejected (0 if never sent)."""
    row = conn.execute(
        "SELECT rejection_count FROM hitl_suppression WHERE card_key = ?",
        (card_key,),
    ).fetchone()
    return row[0] if row else 0


def get_known_issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all active Known Issues (known_issue=1, resolved_at IS NULL)."""
    rows = conn.execute(
        """
        SELECT card_key, card_type, description, first_sent_at, last_action_at,
               rejection_count, known_issue_note
        FROM hitl_suppression
        WHERE known_issue = 1 AND resolved_at IS NULL
        ORDER BY first_sent_at DESC
        """,
    ).fetchall()
    return [
        {
            "card_key": r[0],
            "card_type": r[1],
            "description": r[2],
            "first_sent_at": r[3],
            "last_action_at": r[4],
            "rejection_count": r[5],
            "known_issue_note": r[6] or "",
        }
        for r in rows
    ]


def check_reminders_due(
    conn: sqlite3.Connection, reminder_days: int
) -> list[dict[str, Any]]:
    """Return Known Issues that haven't had any action in reminder_days days."""
    cutoff = time.time() - reminder_days * 86400
    rows = conn.execute(
        """
        SELECT card_key, card_type, description, first_sent_at, last_action_at,
               rejection_count, known_issue_note
        FROM hitl_suppression
        WHERE known_issue = 1
          AND resolved_at IS NULL
          AND (last_action_at IS NULL OR last_action_at < ?)
        ORDER BY first_sent_at ASC
        """,
        (cutoff,),
    ).fetchall()
    return [
        {
            "card_key": r[0],
            "card_type": r[1],
            "description": r[2],
            "first_sent_at": r[3],
            "last_action_at": r[4],
            "rejection_count": r[5],
            "known_issue_note": r[6] or "",
        }
        for r in rows
    ]


def touch_reminder_sent(conn: sqlite3.Connection, card_key: str) -> None:
    """Update last_action_at so the 7-day reminder clock resets."""
    conn.execute(
        "UPDATE hitl_suppression SET last_action_at = ? WHERE card_key = ?",
        (time.time(), card_key),
    )
    conn.commit()
