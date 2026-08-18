"""Tests for utils/hitl_tracker.py — all against an in-memory SQLite DB."""

import sqlite3
import time

import pytest

from utils.hitl_tracker import (
    check_reminders_due,
    get_known_issues,
    get_rejection_count,
    mark_card_acknowledged,
    mark_card_approved,
    mark_card_deferred,
    mark_card_rejected,
    mark_card_resolved,
    mark_card_sent,
    should_send_card,
    stable_nid,
    touch_reminder_sent,
)

_DDL = """
CREATE TABLE IF NOT EXISTS hitl_suppression (
    card_key          TEXT PRIMARY KEY,
    card_type         TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    first_sent_at     REAL NOT NULL DEFAULT 0,
    last_sent_at      REAL NOT NULL DEFAULT 0,
    send_count        INTEGER NOT NULL DEFAULT 1,
    last_action       TEXT,
    last_action_at    REAL,
    rejection_count   INTEGER NOT NULL DEFAULT 0,
    next_allowed_at   REAL,
    known_issue       INTEGER NOT NULL DEFAULT 0,
    known_issue_note  TEXT,
    resolved_at       REAL
)
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(_DDL)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# should_send_card
# ---------------------------------------------------------------------------


def test_send_unknown_card(conn):
    assert should_send_card(conn, "update:foo") is True


def test_send_blocked_after_mark_sent(conn):
    # Card is in the HITL queue — must not re-fire until user acts or condition clears.
    mark_card_sent(conn, "update:foo", "update", "desc")
    assert should_send_card(conn, "update:foo") is False


def test_skip_pending_check_allows_resend_when_in_queue(conn):
    # Periodic-retry callers (e.g. ResourcePoller) can bypass the in-queue guard.
    mark_card_sent(conn, "resource:disk_critical", "disk_recovery", "Disk full")
    assert (
        should_send_card(conn, "resource:disk_critical", skip_pending_check=True)
        is True
    )


def test_skip_pending_check_still_blocks_known_issue(conn):
    mark_card_sent(conn, "resource:disk_critical", "disk_recovery", "Disk full")
    mark_card_acknowledged(conn, "resource:disk_critical")
    assert (
        should_send_card(conn, "resource:disk_critical", skip_pending_check=True)
        is False
    )


def test_skip_pending_check_still_blocks_active_rejection_cooldown(conn):
    mark_card_sent(conn, "resource:disk_critical", "disk_recovery", "Disk full")
    mark_card_rejected(conn, "resource:disk_critical", cooldown_hours=1.0)
    assert (
        should_send_card(conn, "resource:disk_critical", skip_pending_check=True)
        is False
    )


def test_send_blocked_by_cooldown(conn):
    mark_card_sent(conn, "update:foo", "update", "desc")
    mark_card_rejected(conn, "update:foo", cooldown_hours=1.0)
    assert should_send_card(conn, "update:foo") is False


def test_send_allowed_after_cooldown_expires(conn):
    mark_card_sent(conn, "update:foo", "update", "desc")
    mark_card_rejected(conn, "update:foo", cooldown_hours=0.0)
    # cooldown_hours=0 → next_allowed_at = now, which is not > now
    assert should_send_card(conn, "update:foo") is True


def test_send_blocked_by_known_issue(conn):
    mark_card_sent(conn, "resource:disk_critical", "disk_recovery", "Disk full")
    mark_card_acknowledged(conn, "resource:disk_critical")
    assert should_send_card(conn, "resource:disk_critical") is False


def test_send_allowed_after_resolved(conn):
    mark_card_sent(conn, "resource:disk_critical", "disk_recovery", "Disk full")
    mark_card_acknowledged(conn, "resource:disk_critical")
    mark_card_resolved(conn, "resource:disk_critical")
    assert should_send_card(conn, "resource:disk_critical") is True


# ---------------------------------------------------------------------------
# mark_card_sent upsert
# ---------------------------------------------------------------------------


def test_mark_card_sent_inserts(conn):
    mark_card_sent(conn, "k", "t", "d")
    row = conn.execute(
        "SELECT send_count FROM hitl_suppression WHERE card_key='k'"
    ).fetchone()
    assert row[0] == 1


def test_mark_card_sent_increments(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_sent(conn, "k", "t", "d")
    row = conn.execute(
        "SELECT send_count FROM hitl_suppression WHERE card_key='k'"
    ).fetchone()
    assert row[0] == 2


def test_mark_card_sent_clears_resolved(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_resolved(conn, "k")
    mark_card_sent(conn, "k", "t", "d")
    row = conn.execute(
        "SELECT resolved_at FROM hitl_suppression WHERE card_key='k'"
    ).fetchone()
    assert row[0] is None


# ---------------------------------------------------------------------------
# mark_card_rejected — cooldown doubling
# ---------------------------------------------------------------------------


def test_reject_sets_next_allowed(conn):
    mark_card_sent(conn, "k", "t", "d")
    before = time.time()
    mark_card_rejected(conn, "k", cooldown_hours=1.0)
    after = time.time()
    row = conn.execute(
        "SELECT rejection_count, next_allowed_at FROM hitl_suppression WHERE card_key='k'"
    ).fetchone()
    rejection_count, next_allowed_at = row
    assert rejection_count == 1
    assert next_allowed_at >= before + 3600
    assert next_allowed_at <= after + 3600


def test_reject_doubles_cooldown(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_rejected(conn, "k", cooldown_hours=24.0)
    before = time.time()
    mark_card_rejected(conn, "k", cooldown_hours=24.0)
    row = conn.execute(
        "SELECT rejection_count, next_allowed_at FROM hitl_suppression WHERE card_key='k'"
    ).fetchone()
    count, naa = row
    assert count == 2
    # 2nd rejection → 2 × 24 h = 48 h
    assert naa >= before + 48 * 3600 - 1


def test_rejection_count_starts_zero_for_unsent(conn):
    assert get_rejection_count(conn, "never_seen") == 0


# ---------------------------------------------------------------------------
# mark_card_acknowledged
# ---------------------------------------------------------------------------


def test_acknowledge_sets_known_issue(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_acknowledged(conn, "k")
    row = conn.execute(
        "SELECT known_issue, next_allowed_at FROM hitl_suppression WHERE card_key='k'"
    ).fetchone()
    assert row[0] == 1
    assert row[1] is None


def test_acknowledge_for_unsent_card(conn):
    mark_card_acknowledged(conn, "k2")
    assert should_send_card(conn, "k2") is False


# ---------------------------------------------------------------------------
# mark_card_deferred
# ---------------------------------------------------------------------------


def test_deferred_blocks_until_expiry(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_deferred(conn, "k", hours=24.0)
    assert should_send_card(conn, "k") is False


def test_deferred_zero_hours_allows_send(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_deferred(conn, "k", hours=0.0)
    assert should_send_card(conn, "k") is True


# ---------------------------------------------------------------------------
# mark_card_resolved
# ---------------------------------------------------------------------------


def test_resolved_clears_known_issue(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_acknowledged(conn, "k")
    mark_card_resolved(conn, "k")
    row = conn.execute(
        "SELECT known_issue, resolved_at FROM hitl_suppression WHERE card_key='k'"
    ).fetchone()
    assert row[0] == 0
    assert row[1] is not None


# ---------------------------------------------------------------------------
# get_known_issues
# ---------------------------------------------------------------------------


def test_get_known_issues_empty(conn):
    assert get_known_issues(conn) == []


def test_get_known_issues_returns_active(conn):
    mark_card_sent(conn, "resource:disk_critical", "disk_recovery", "Disk full")
    mark_card_acknowledged(conn, "resource:disk_critical")
    issues = get_known_issues(conn)
    assert len(issues) == 1
    assert issues[0]["card_key"] == "resource:disk_critical"


def test_get_known_issues_excludes_resolved(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_acknowledged(conn, "k")
    mark_card_resolved(conn, "k")
    assert get_known_issues(conn) == []


# ---------------------------------------------------------------------------
# check_reminders_due
# ---------------------------------------------------------------------------


def test_reminders_due_empty(conn):
    assert check_reminders_due(conn, 7) == []


def test_reminders_due_returns_old_issues(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_acknowledged(conn, "k")
    # Force last_action_at to be older than reminder_days
    conn.execute(
        "UPDATE hitl_suppression SET last_action_at = ? WHERE card_key = 'k'",
        (time.time() - 8 * 86400,),
    )
    conn.commit()
    due = check_reminders_due(conn, 7)
    assert len(due) == 1
    assert due[0]["card_key"] == "k"


def test_reminders_due_not_returned_for_recent_action(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_acknowledged(conn, "k")
    due = check_reminders_due(conn, 7)
    assert due == []


def test_touch_reminder_resets_clock(conn):
    mark_card_sent(conn, "k", "t", "d")
    mark_card_acknowledged(conn, "k")
    conn.execute(
        "UPDATE hitl_suppression SET last_action_at = ? WHERE card_key = 'k'",
        (time.time() - 8 * 86400,),
    )
    conn.commit()
    touch_reminder_sent(conn, "k")
    due = check_reminders_due(conn, 7)
    assert due == []


# ---------------------------------------------------------------------------
# stable_nid
# ---------------------------------------------------------------------------


def test_stable_nid_deterministic():
    assert stable_nid("update:foo") == stable_nid("update:foo")


def test_stable_nid_different_keys_produce_different_nids():
    assert stable_nid("update:foo") != stable_nid("update:bar")


def test_stable_nid_length():
    assert len(stable_nid("resource:disk_critical")) == 24


def test_stable_nid_is_hex():
    nid = stable_nid("log_triage:abc123")
    assert all(c in "0123456789abcdef" for c in nid)


# ---------------------------------------------------------------------------
# mark_card_approved
# ---------------------------------------------------------------------------


def test_mark_card_approved_sets_resolved_at(conn):
    mark_card_sent(conn, "update:foo", "update", "desc")
    mark_card_approved(conn, "update:foo")
    row = conn.execute(
        "SELECT last_action, resolved_at FROM hitl_suppression WHERE card_key = 'update:foo'"
    ).fetchone()
    assert row[0] == "approved"
    assert row[1] is not None


def test_mark_card_approved_allows_next_send(conn):
    mark_card_sent(conn, "update:foo", "update", "desc")
    mark_card_approved(conn, "update:foo")
    # resolved_at set → should_send_card must return True so a fresh card can fire
    assert should_send_card(conn, "update:foo") is True


def test_mark_card_approved_does_not_set_known_issue(conn):
    mark_card_sent(conn, "update:bar", "update", "desc")
    mark_card_approved(conn, "update:bar")
    row = conn.execute(
        "SELECT known_issue FROM hitl_suppression WHERE card_key = 'update:bar'"
    ).fetchone()
    assert row[0] == 0


def test_mark_card_approved_on_new_key_inserts_row(conn):
    mark_card_approved(conn, "update:new")
    row = conn.execute(
        "SELECT last_action, resolved_at FROM hitl_suppression WHERE card_key = 'update:new'"
    ).fetchone()
    assert row is not None
    assert row[0] == "approved"
    assert row[1] is not None
