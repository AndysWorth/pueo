"""DB-backed approval card suppression: rejection memory, cooldown, and Known Issues."""

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


def stable_nid(suppression_key: str) -> str:
    """Deterministic notification_id derived from suppression_key.

    Using a stable id means FileNotifier.send() overwrites the same file rather
    than accumulating new UUID-named files when a poller re-fires in error.
    """
    return hashlib.sha256(suppression_key.encode()).hexdigest()[:24]


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
    # Card is in the approval queue (sent, no action yet) — do not re-fire.
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
            description = excluded.description,
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


_STUCK_BACKOFF_HOURS: float = 0.5  # 30-minute back-off when a loop gets stuck


def mark_investigation_backoff(conn: sqlite3.Connection, card_key: str) -> None:
    """Temporarily defer re-investigation when AgentLoop exits without terminal tool.

    Prevents the poll loop from immediately re-triggering on the same event after a
    stuck/exhausted/failed outcome. Expires after _STUCK_BACKOFF_HOURS.
    """
    mark_card_deferred(conn, card_key, hours=_STUCK_BACKOFF_HOURS)


def mark_card_approved(conn: sqlite3.Connection, card_key: str) -> None:
    """Mark card as approved; treats this occurrence as resolved so future polls can re-fire."""
    now = time.time()
    conn.execute(
        """
        INSERT INTO hitl_suppression
            (card_key, card_type, description, first_sent_at, last_sent_at,
             last_action, last_action_at, resolved_at)
        VALUES (?, '', '', ?, ?, 'approved', ?, ?)
        ON CONFLICT(card_key) DO UPDATE SET
            last_action = 'approved',
            last_action_at = excluded.last_action_at,
            resolved_at = excluded.resolved_at
        """,
        (card_key, now, now, now, now),
    )
    conn.commit()


def mark_card_auto_applied(
    conn: sqlite3.Connection, card_key: str, card_type: str, description: str
) -> None:
    """Record that Pueo applied this action itself without an approval card.

    The row is suppressed (should_send_card returns False) but never "pending",
    so when the condition clears the poll resolves it silently instead of
    attributing the change to something outside Pueo (#843).
    """
    now = time.time()
    conn.execute(
        """
        INSERT INTO hitl_suppression
            (card_key, card_type, description, first_sent_at, last_sent_at,
             last_action, last_action_at)
        VALUES (?, ?, ?, ?, ?, 'auto_applied', ?)
        ON CONFLICT(card_key) DO UPDATE SET
            last_sent_at = excluded.last_sent_at,
            send_count = send_count + 1,
            card_type = excluded.card_type,
            description = excluded.description,
            last_action = 'auto_applied',
            last_action_at = excluded.last_action_at,
            resolved_at = NULL,
            next_allowed_at = NULL
        """,
        (card_key, card_type, description, now, now, now),
    )
    conn.commit()


def get_known_issue_note(conn: sqlite3.Connection, card_key: str) -> Optional[str]:
    """Return the stored known_issue_note for an active Known Issue, or None."""
    row = conn.execute(
        "SELECT known_issue_note FROM hitl_suppression"
        " WHERE card_key = ? AND known_issue = 1 AND resolved_at IS NULL",
        (card_key,),
    ).fetchone()
    return row[0] if row else None


def mark_card_resolved(conn: sqlite3.Connection, card_key: str) -> None:
    """Mark condition as resolved; next occurrence starts fresh."""
    now = time.time()
    conn.execute(
        "UPDATE hitl_suppression SET resolved_at = ?, known_issue = 0 WHERE card_key = ?",
        (now, card_key),
    )
    conn.commit()


def resolve_card(
    watch_dir: str,
    nid: str,
    *,
    db_path: Optional[str] = None,
    card_key: Optional[str] = None,
) -> bool:
    """Touch <nid>.resolved in watch_dir and optionally update the suppression row.

    The .resolved sentinel is always written (the dashboard uses it to close
    pending cards and it is harmless when no card is present).  Returns True
    when the .json card file was also found, False when it was absent.
    """
    card_path = Path(watch_dir) / f"{nid}.json"
    card_present = card_path.exists()
    resolved_path = Path(watch_dir) / f"{nid}.resolved"
    try:
        resolved_path.touch()
    except Exception:  # nosec B110
        pass
    if db_path is not None and card_key is not None:
        try:
            with sqlite3.connect(db_path) as _conn:
                mark_card_resolved(_conn, card_key)
        except Exception:  # nosec B110
            pass
    if card_present:
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                "INFO",
                "hitl",
                f"Card auto-resolved: {card_key or nid}",
                {"nid": nid},
            )
        except Exception:  # nosec B110
            pass
    return card_present


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


def was_card_actioned(conn: sqlite3.Connection, card_key: str) -> bool:
    """Return True if a human approved the card (last_action = 'approved').

    Used to distinguish a self-recovery (device came back without any HA action)
    from a human-actioned recovery.  Returns False when the row is absent (card
    was suppressed or never sent) or when the last action was anything other than
    'approved'.
    """
    row = conn.execute(
        "SELECT last_action FROM hitl_suppression WHERE card_key = ?",
        (card_key,),
    ).fetchone()
    return bool(row and row[0] == "approved")
