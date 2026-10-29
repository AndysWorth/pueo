#!/usr/bin/env python3
"""Layer 4 — continuous log streaming via 'ha core logs --follow' with two-layer AI triage and repair triggering."""

import asyncio
import collections
import dataclasses
import datetime
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from pydantic import BaseModel, Field

import config as _config
from config import (
    DB_PATH,
    HA_API_PORT,
    HA_API_TOKEN,
    HA_DISK_CRITICAL_GB,
    HA_DISK_WARN_GB,
    HA_HOST,
    HA_MEM_WARN_MB,
    HA_NOTIFICATION_POLL_INTERVAL_MINUTES,
    HA_REPAIR_POLL_INTERVAL_MINUTES,
    HA_UPDATE_CHECK_INTERVAL_HOURS,
    HA_UPDATE_NOTIFY_ON_AVAILABLE,
    HA_UPDATE_RELEASE_NOTES_CACHE_DIR,
    HA_USER,
    LOG_TRIAGE_COOLDOWN_HOURS,
    MAX_PROMPT_TOKENS,
    MAX_REPAIRS_PER_HOUR,
    NETALERTX_API_PORT,
    NETALERTX_API_TOKEN,
    NETALERTX_HOST,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    AUTONOMY_LEVEL,
    CONFIDENCE_THRESHOLD,
    DEBOUNCE_WINDOW_SECONDS,
    REPAIR_COOLDOWN_SECONDS,
    RESOURCE_POLL_INTERVAL_SECONDS,
    SELF_HEALING_ENABLED,
    SSH_KEY_PATH,
    SSH_RETRY_BASE_DELAY,
)
from interfaces import (
    HAEventSubscriberProtocol,
    HARestClientProtocol,
    HAWebSocketClientProtocol,
    LLMClientProtocol,
    NetAlertXClientProtocol,
    SSHClientProtocol,
)
from utils.core.context import estimate_tokens, sliding_window_lines
from utils.hitl.llm_trace import LLMTrace
from utils.core.logging import get_logger, setup_logging, set_correlation_id
from utils.llm.llm_factory import _default_model_for_provider, make_llm_client
from utils.core.prompts import load_prompt, repeat_query
from utils.agent.autonomy import AutonomyGate, RiskLevel
from utils.hitl.notify import NotifierProtocol, get_notifier
from utils.core.rate_limiter import Debouncer, RateLimiter, RateLimitExceeded
from utils.ha.ha_rest_client import (
    HARepairIssue,
    HARestClient,
    UpdateStatus,
    get_update_status,
)
from utils.disk.resource import ResourcePoller
from utils.core.retry import async_retry
from utils.ha.ssh_client import AsyncSSHClient

log = get_logger("ha_log_monitor")


def _poll_sleep_seconds(
    base_interval: float,
    event_subscriber: Optional[HAEventSubscriberProtocol],
    backoff: float = 0,
) -> float:
    """Return effective sleep duration for a subscriber-aware poll loop.

    Uses ``HA_EVENT_FALLBACK_POLL_MINUTES`` when the subscriber is connected and
    there is no active backoff.  Falls back to *backoff* (if set) or *base_interval*.
    """
    import config as _cfg

    if (
        backoff == 0
        and event_subscriber is not None
        and event_subscriber.is_connected()
        and _cfg.HA_EVENT_FALLBACK_POLL_MINUTES > 0
    ):
        return _cfg.HA_EVENT_FALLBACK_POLL_MINUTES * 60
    return backoff if backoff else base_interval


_debouncer = Debouncer(DEBOUNCE_WINDOW_SECONDS)
_rate_limiter = RateLimiter(MAX_REPAIRS_PER_HOUR, 3600)

_LOOP_NAME = "ha_log_monitor"
_RETENTION_SECONDS = 31 * 24 * 3600  # 31 days


@dataclasses.dataclass
class _LogStreamState:
    """Per-source sparkline state — one instance per always-on SSH tail stream."""

    loop_name: str
    buckets: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=60)
    )
    current_minute: int = 0
    bucket_total: int = 0
    bucket_matches: int = 0
    bucket_min_ts: int = 0  # epoch of first log line in current bucket (0 = none yet)
    last_line_at: float = 0.0
    last_match_at: float = 0.0
    # Replay protection (#975): `ha core logs --follow` replays its backlog on
    # every reconnect.  Per-source so the core and supervisor streams never mix.
    log_buffer: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=50)
    )
    entry_ts: Optional[float] = None  # HA timestamp of the current log entry
    watermark: Optional[float] = None  # newest entry timestamp already triaged
    seen_at_watermark: set[str] = dataclasses.field(default_factory=set)


_core_state = _LogStreamState(loop_name=_LOOP_NAME)
_supervisor_state = _LogStreamState(loop_name="ha_log_monitor_supervisor")


def _get_stream_state(source: str) -> _LogStreamState:
    return _supervisor_state if source == "supervisor" else _core_state


def _source_to_command(source: str) -> str:
    return (
        "ha supervisor logs --follow"
        if source == "supervisor"
        else "ha core logs --follow"
    )


def _source_to_loop_name(source: str) -> str:
    return "ha_log_monitor_supervisor" if source == "supervisor" else _LOOP_NAME


def _triage_check_and_record(fingerprint: str, now: float) -> bool:
    """Return True if a new HITL card should be sent for this fingerprint.

    Records the triage event and checks the cooldown window. Sync — call via
    asyncio.to_thread from the async log-stream loop.
    """
    from .ha_agent_advanced import record_log_triage_seen, should_send_log_triage_hitl

    with sqlite3.connect(_config.DB_PATH) as _conn:
        record_log_triage_seen(_conn, fingerprint, now)
        return bool(
            should_send_log_triage_hitl(_conn, fingerprint, LOG_TRIAGE_COOLDOWN_HOURS)
        )


def _triage_mark_hitl_sent(fingerprint: str, now: float) -> None:
    """Mark a log-triage fingerprint as HITL-sent. Sync — call via asyncio.to_thread."""
    from .ha_agent_advanced import mark_log_triage_hitl_sent

    with sqlite3.connect(_config.DB_PATH) as _conn:
        mark_log_triage_hitl_sent(_conn, fingerprint, now)


def _update_check_should_send(suppression_key: str) -> bool:
    """Return True if an update card should be sent for suppression_key."""
    from utils.hitl.hitl_tracker import should_send_card

    with sqlite3.connect(_config.DB_PATH) as _conn:
        return bool(should_send_card(_conn, suppression_key))


def _update_mark_card_sent(
    suppression_key: str, card_type: str, description: str
) -> None:
    """Mark an update card as sent in hitl_suppression."""
    from utils.hitl.hitl_tracker import mark_card_sent

    with sqlite3.connect(_config.DB_PATH) as _conn:
        mark_card_sent(_conn, suppression_key, card_type, description)


def _update_mark_auto_applied(
    suppression_key: str, card_type: str, description: str
) -> None:
    """Mark an update as auto-applied by Pueo in hitl_suppression."""
    from utils.hitl.hitl_tracker import mark_card_auto_applied

    with sqlite3.connect(_config.DB_PATH) as _conn:
        mark_card_auto_applied(_conn, suppression_key, card_type, description)


def _update_already_analyzed(analyzed_key: str) -> bool:
    """Return True if this specific version was already analyzed with no card needed."""
    with sqlite3.connect(_config.DB_PATH) as _conn:
        row = _conn.execute(
            "SELECT resolved_at FROM hitl_suppression WHERE card_key = ?",
            (analyzed_key,),
        ).fetchone()
    return row is not None and row[0] is None


def _update_check_pending(suppression_key: str) -> bool:
    """Return True if an update card for suppression_key is currently pending."""
    with sqlite3.connect(_config.DB_PATH) as _conn:
        row = _conn.execute(
            "SELECT send_count, last_action, resolved_at"
            " FROM hitl_suppression WHERE card_key = ?",
            (suppression_key,),
        ).fetchone()
    return row is not None and row[0] > 0 and row[1] is None and row[2] is None


def _update_resolve_card(suppression_key: str) -> None:
    """Mark an update card as resolved in hitl_suppression."""
    from utils.hitl.hitl_tracker import mark_card_resolved

    entity_id = suppression_key.removeprefix("update:")
    with sqlite3.connect(_config.DB_PATH) as _conn:
        mark_card_resolved(_conn, suppression_key)
        _conn.execute(
            "UPDATE hitl_suppression SET resolved_at = ?"
            " WHERE card_key LIKE ? AND resolved_at IS NULL",
            (time.time(), f"update_analyzed:{entity_id}:%"),
        )


def _close_installed_update_card(suppression_key: str, watch_dir: str) -> bool:
    """Write .approved for an approved card whose update is now gone but has no sentinel.

    Acts only when all four conditions hold: the .json card exists, no sentinel
    (.approved/.rejected/.deferred/.resolved) exists, the DB row has
    last_action='approved', and the entity is not currently executing.  Returns True
    when it writes the sentinel, False otherwise.
    """
    from utils.hitl.hitl_tracker import stable_nid

    entity_id = suppression_key.removeprefix("update:")
    try:
        from agents.ha_update_manager import is_entity_executing_update

        if is_entity_executing_update(entity_id):
            return False
    except Exception:  # nosec B110
        pass

    with sqlite3.connect(_config.DB_PATH) as _conn:
        row = _conn.execute(
            "SELECT last_action, resolved_at FROM hitl_suppression WHERE card_key = ?",
            (suppression_key,),
        ).fetchone()
    if row is None or row[0] != "approved" or row[1] is not None:
        return False

    nid = stable_nid(suppression_key)
    watch = Path(watch_dir)
    card_path = watch / f"{nid}.json"
    if not card_path.exists():
        return False
    for ext in (".approved", ".rejected", ".deferred", ".resolved"):
        if (watch / f"{nid}{ext}").exists():
            return False

    try:
        data = json.loads(card_path.read_text())
        data["fix_applied"] = True
        card_path.write_text(json.dumps(data, indent=2))
    except Exception:  # nosec B110
        pass
    try:
        (watch / f"{nid}.approved").touch()
    except Exception:  # nosec B110
        return False
    log.info(
        "update_interrupted_approval_closed",
        suppression_key=suppression_key,
        entity_id=entity_id,
    )
    return True


def _update_sweep_absent_pending(seen_keys: set) -> list:
    """Return (card_key,) rows for update cards no longer present in HA."""
    with sqlite3.connect(_config.DB_PATH) as _conn:
        rows = _conn.execute(
            "SELECT card_key FROM hitl_suppression"
            " WHERE card_key LIKE 'update:%'"
            "   AND send_count > 0 AND last_action IS NULL AND resolved_at IS NULL"
        ).fetchall()
    return rows


def _init_sparkline_db(state: _LogStreamState) -> None:
    """Pre-populate ring buffer from SQLite and prune old rows."""
    try:
        with sqlite3.connect(_config.DB_PATH) as conn:
            conn.execute(
                "DELETE FROM stream_metrics WHERE loop_name = ? AND bucket_ts < ?",
                (state.loop_name, int(time.time()) - _RETENTION_SECONDS),
            )
            rows = conn.execute(
                "SELECT bucket_ts, total_lines, match_lines FROM stream_metrics "
                "WHERE loop_name = ? ORDER BY bucket_ts DESC LIMIT 60",
                (state.loop_name,),
            ).fetchall()
        for row in reversed(rows):
            state.buckets.append((row[0], row[1], row[2]))
        state.current_minute = int(time.time() // 60)
    except Exception:  # nosec B110
        pass


def _persist_sparkline_bucket(
    loop_name: str, bucket_ts: int, total: int, matches: int, min_line_ts: int
) -> None:
    try:
        with sqlite3.connect(_config.DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO stream_metrics "
                "(loop_name, bucket_ts, total_lines, match_lines, min_line_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (loop_name, bucket_ts, total, matches, min_line_ts or None),
            )
    except Exception:  # nosec B110
        pass


def _record_sparkline_line(state: _LogStreamState, matched: bool) -> None:
    """Update sparkline ring buffer for one received log line."""
    now = time.time()
    state.last_line_at = now
    if matched:
        state.last_match_at = now

    current_min = int(now // 60)
    if current_min != state.current_minute:
        completed_ts = state.current_minute * 60
        state.buckets.append((completed_ts, state.bucket_total, state.bucket_matches))
        _persist_sparkline_bucket(
            state.loop_name,
            completed_ts,
            state.bucket_total,
            state.bucket_matches,
            state.bucket_min_ts,
        )
        state.bucket_total = 0
        state.bucket_matches = 0
        state.bucket_min_ts = 0
        state.current_minute = current_min

    if state.bucket_total == 0:
        state.bucket_min_ts = int(now)
    state.bucket_total += 1
    if matched:
        state.bucket_matches += 1


def get_sparkline_data_for_loop(
    loop_name: str, bucket_size: str = "1m", time_range: str = "1h"
) -> dict:
    """Return sparkline data for any stream_metrics loop_name."""
    now_ts = int(time.time())
    range_seconds = {
        "1h": 3600,
        "6h": 6 * 3600,
        "24h": 24 * 3600,
        "7d": 7 * 86400,
        "30d": 30 * 86400,
    }.get(time_range, 3600)
    bucket_seconds = {"1m": 60, "1h": 3600, "1d": 86400}.get(bucket_size, 60)
    cutoff_ts = now_ts - range_seconds

    try:
        with sqlite3.connect(_config.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT bucket_ts, total_lines, match_lines, min_line_ts "
                "FROM stream_metrics "
                "WHERE loop_name = ? ORDER BY bucket_ts",
                (loop_name,),
            ).fetchall()
    except Exception:  # nosec B110
        rows = []

    # Aggregate into requested bucket size; track min_line_ts per aggregated key
    agg: dict = {}
    for row_ts, row_total, row_matches, row_min_ts in rows:
        key = (row_ts // bucket_seconds) * bucket_seconds
        if key not in agg:
            agg[key] = [0, 0, None]
        agg[key][0] += row_total
        agg[key][1] += row_matches
        if row_min_ts is not None:
            if agg[key][2] is None or row_min_ts < agg[key][2]:
                agg[key][2] = row_min_ts

    # Append the in-progress current bucket from the matching state object
    state: Optional[_LogStreamState] = None
    if loop_name == _LOOP_NAME:
        state = _core_state
    elif loop_name == "ha_log_monitor_supervisor":
        state = _supervisor_state
    if state is not None and state.current_minute:
        cur_key = (state.current_minute * 60 // bucket_seconds) * bucket_seconds
        if cur_key not in agg:
            agg[cur_key] = [0, 0, None]
        agg[cur_key][0] += state.bucket_total
        agg[cur_key][1] += state.bucket_matches
        if state.bucket_min_ts:
            if agg[cur_key][2] is None or state.bucket_min_ts < agg[cur_key][2]:
                agg[cur_key][2] = state.bucket_min_ts

    # Fill empty bucket slots so every time position is represented (cap at 1440).
    # Start from the earliest actual data so panning can reach all history.
    first_key = (
        (min(agg.keys()) // bucket_seconds) * bucket_seconds
        if agg
        else (cutoff_ts // bucket_seconds) * bucket_seconds
    )
    end_key = (now_ts // bucket_seconds) * bucket_seconds
    slot = first_key
    slot_count = 0
    while slot <= end_key and slot_count < 1440:
        if slot not in agg:
            agg[slot] = [0, 0, None]
        slot += bucket_seconds
        slot_count += 1

    # display_ts: use min_line_ts when available, else fall back to bucket key
    buckets = [
        [v[2] if v[2] is not None else k, v[0], v[1]] for k, v in sorted(agg.items())
    ]
    last_line_at = state.last_line_at if state is not None else 0.0
    last_match_at = state.last_match_at if state is not None else 0.0
    return {
        "buckets": buckets,
        "bucket_size": bucket_size,
        "time_range": time_range,
        "last_line_at": last_line_at,
        "last_match_at": last_match_at,
    }


def get_ha_log_sparkline_data(bucket_size: str = "1m", time_range: str = "1h") -> dict:
    """Return sparkline data for the HA core log monitor (backward-compat wrapper)."""
    return get_sparkline_data_for_loop(
        _LOOP_NAME, bucket_size=bucket_size, time_range=time_range
    )


# High-priority regex to capture structural components collapsing or syntax crashes.
# "Uncaught thread exception" is severe enough to match without an ERROR/CRITICAL prefix.
CRITICAL_LOG_PATTERN = re.compile(
    r"((ERROR|CRITICAL).*?(Component error|Failed to initialize|Traceback|Invalid config|Error doing job)"
    r"|Uncaught thread exception)",
    re.IGNORECASE,
)

# "Traceback (most recent call last):" appears on its own line in multi-line Python
# exceptions.  It is critical only when the preceding log line was ERROR or CRITICAL.
_ERROR_CRITICAL_PREFIX = re.compile(r"\b(ERROR|CRITICAL)\b", re.IGNORECASE)
_TRACEBACK_HEADER = re.compile(r"^Traceback \(most recent call last\)", re.IGNORECASE)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HA_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s")


def _strip_ansi(line: str) -> str:
    """Remove ANSI colour escapes that `ha core logs` wraps around each entry."""
    return _ANSI_ESCAPE.sub("", line)


def _parse_ha_log_ts(line: str) -> Optional[float]:
    """Return the epoch of a `YYYY-MM-DD HH:MM:SS.fff` HA log prefix, else None.

    HA logs in local time without an offset, so the value is only comparable
    with other values from the same host.  Continuation lines (traceback
    frames) have no prefix and return None.
    """
    m = _HA_LOG_TS.match(_strip_ansi(line))
    if not m:
        return None
    text = m.group(1)
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in text else "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.datetime.strptime(text, fmt).timestamp()
    except ValueError:
        return None


def _replay_gate(state: _LogStreamState, line_fp: str) -> bool:
    """Return True if a critical line is new; False if it is a backlog replay.

    Advances `state.watermark` / `state.seen_at_watermark` for new lines.
    Lines whose entry has no resolvable timestamp always pass (fail open).
    """
    ts = state.entry_ts
    if ts is None:
        return True
    if state.watermark is not None:
        if ts < state.watermark:
            return False
        if ts == state.watermark:
            if line_fp in state.seen_at_watermark:
                return False
            state.seen_at_watermark.add(line_fp)
            return True
    state.watermark = ts
    state.seen_at_watermark = {line_fp}
    return True


def _watermark_key(source: str) -> str:
    return f"log_monitor:watermark:{source}"


def _load_watermark(source: str) -> Optional[tuple[float, set[str]]]:
    """Load the persisted (watermark, seen fingerprints) for `source`, or None."""
    try:
        with sqlite3.connect(_config.DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE key = ?", (_watermark_key(source),)
            ).fetchone()
        if row:
            data = json.loads(row[0])
            return float(data["ts"]), set(data.get("fps", []))
    except Exception as e:  # nosec B110
        log.warning("log_watermark_load_failed", source=source, error=str(e))
    return None


def _save_watermark(source: str, ts: float, fps: list[str]) -> None:
    """Persist the watermark for `source` in user_prefs (no migration needed)."""
    try:
        with sqlite3.connect(_config.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_watermark_key(source), json.dumps({"ts": ts, "fps": fps})),
            )
    except Exception as e:  # nosec B110
        log.warning("log_watermark_save_failed", source=source, error=str(e))


def _restore_watermark(state: _LogStreamState, source: str) -> None:
    """Adopt the persisted watermark when it is newer than the in-memory one."""
    loaded = _load_watermark(source)
    if loaded is None:
        return
    ts, fps = loaded
    if state.watermark is None or ts > state.watermark:
        state.watermark = ts
        state.seen_at_watermark = fps


def _is_critical_line(line: str, prev_line: str) -> bool:
    """Return True if `line` should trigger triage.

    Extends CRITICAL_LOG_PATTERN with one additional case: a bare
    "Traceback (most recent call last):" header is critical when the
    immediately preceding buffered line contained ERROR or CRITICAL.
    """
    if CRITICAL_LOG_PATTERN.search(line):
        return True
    return bool(
        _TRACEBACK_HEADER.match(line) and _ERROR_CRITICAL_PREFIX.search(prev_line)
    )


# OSError errno values that indicate a normal SSH session tear-down by the remote host.
# These are expected during long-running log streams and warrant WARNING, not ERROR.
_TRANSIENT_SSH_ERRNOS: frozenset[int] = frozenset({32, 54, 104})  # EPIPE, ECONNRESET

# Log content patterns that indicate a transient external condition (network outage,
# upstream API unavailable) rather than a fixable config error. When the error block
# contains any of these, skip the LLM triage call and emit a WARNING timeline event.
_TRANSIENT_LOG_PATTERNS = re.compile(
    r"(NameResolutionError|Failed to resolve"
    r"|MaxRetryError|Max retries exceeded"
    r"|ConnectionError|ConnectionRefusedError"
    r"|TimeoutError|timed out"
    r"|Status Code: 50[2-4]|Gateway Timeout"
    r"|urllib3\.exceptions)",
    re.IGNORECASE,
)

# Number of occurrences of the same (logger, first-line) fingerprint within
# _LOG_RECURRENCE_WINDOW seconds before the error is forced through triage even
# when its block matches _TRANSIENT_LOG_PATTERNS.
LOG_RECURRENCE_THRESHOLD = 3
_LOG_RECURRENCE_WINDOW = 24 * 3600  # seconds
_LOG_BLOCK_MAX_LINES = 50
_LOG_BLOCK_MAX_SECONDS = 2.0

_HA_LOG_LOGGER_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_.]*)\]")
_EXCEPTION_LINE_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_.]*(?:Error|Exception|Warning|Fault)):"
)


def _extract_logger(line: str) -> Optional[str]:
    """Return the first bracketed logger name from a HA log line, or None."""
    m = _HA_LOG_LOGGER_RE.search(_strip_ansi(line))
    return m.group(1) if m else None


def _extract_exception_class(block: list[str]) -> Optional[str]:
    """Return the short exception class name from the last matching line of *block*."""
    result = None
    for line in block:
        m = _EXCEPTION_LINE_RE.match(line.strip())
        if m:
            result = m.group(1).rsplit(".", 1)[-1]
    return result


def _strip_log_timestamp(line: str) -> str:
    """Remove the 'YYYY-MM-DD HH:MM:SS ' prefix from a HA log line for stable hashing."""
    m = _HA_LOG_TS.match(_strip_ansi(line))
    return line[m.end() :] if m else line


def _check_and_record_recurrence(logger: str, first_error_line: str, now: float) -> int:
    """Record this error occurrence and return count within _LOG_RECURRENCE_WINDOW. Sync.

    *first_error_line* should be the message without its timestamp prefix so
    that repeated occurrences at different times share the same fingerprint.
    """
    fp = hashlib.sha256(f"{logger}|{first_error_line}".encode()).hexdigest()[:16]
    key = f"log_recurrence:{fp}"
    cutoff = now - _LOG_RECURRENCE_WINDOW
    try:
        with sqlite3.connect(_config.DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE key = ?", (key,)
            ).fetchone()
            timestamps: list[float] = json.loads(row[0]) if row else []
            timestamps = [t for t in timestamps if t > cutoff]
            timestamps.append(now)
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(timestamps)),
            )
    except Exception as e:  # nosec B110
        log.warning("log_recurrence_record_failed", error=str(e))
        return 1  # fail safe: treat as single occurrence
    return len(timestamps)


async def _collect_error_block(
    aiter: AsyncIterator[str],
    first_line: str,
    log_buffer: collections.deque,
) -> tuple[list[str], Optional[str]]:
    """Collect traceback continuation lines that belong to the same log entry.

    Reads from *aiter* until a timestamped line (start of a new entry) or
    _LOG_BLOCK_MAX_SECONDS / _LOG_BLOCK_MAX_LINES is reached.  Continuation
    lines are appended to *log_buffer*; the boundary raw line is NOT added
    (the main loop handles it on the next iteration via its pending slot).
    Returns (block_lines, boundary_raw | None).
    """
    block = [first_line]
    deadline = asyncio.get_event_loop().time() + _LOG_BLOCK_MAX_SECONDS
    while len(block) <= _LOG_BLOCK_MAX_LINES:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(aiter.__anext__(), timeout=remaining)
        except (StopAsyncIteration, asyncio.TimeoutError):
            break
        stripped = raw.strip()
        if _parse_ha_log_ts(stripped) is not None:
            return block, raw
        log_buffer.append(stripped)
        block.append(stripped)
    return block, None


# ==========================================
# DATA SHAPE DEFINITIONS
# ==========================================
class LogEvaluation(BaseModel):
    is_actionable: bool = Field(
        description="True if this log indicates a configuration or integration issue that a code patch can fix."
    )
    root_cause_summary: str = Field(
        description="Brief string summarizing exactly what failed (e.g., 'Malformed YAML in light integration')."
    )
    confidence_score: float = Field(
        description="Value between 0.0 and 1.0 evaluating certainty."
    )


# ==========================================
# LOCAL REAL-TIME LOG FILTERING ENGINE
# ==========================================
async def analyze_log_line_with_ai(
    recent_lines: list[str],
    llm_client: Optional[LLMClientProtocol] = None,
) -> tuple[LogEvaluation, LLMTrace]:
    """Uses local Ollama to classify whether recent log context contains a patchable error."""
    client = llm_client or make_llm_client()

    system_prompt = load_prompt("triage_log")
    user_envelope = "Evaluate these log lines:\n```\n\n```"
    # repeat_query appends the system prompt to the end of the user message to
    # prevent "lost in the middle" failures when the log context is long.
    repeated_system = repeat_query(system_prompt, "")
    overhead = (
        estimate_tokens(system_prompt)
        + estimate_tokens(user_envelope)
        + estimate_tokens(repeated_system)
    )
    windowed = sliding_window_lines(recent_lines, MAX_PROMPT_TOKENS - overhead)
    log_context = "\n".join(windowed)
    user_prompt = repeat_query(
        system_prompt, f"Evaluate these log lines:\n```\n{log_context}\n```"
    )

    model = _default_model_for_provider()
    _triage_armed = False
    try:
        from utils.agent.supervisor import (
            decrement_active_triage,
            increment_active_triage,
            publish_event,
        )

        increment_active_triage()
        publish_event({"event_type": "triage_start"})
        _triage_armed = True
    except Exception:  # nosec B110
        pass
    _t0 = __import__("time").monotonic()
    try:
        from utils.llm.model_options import one_shot_options, one_shot_think

        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options=one_shot_options(model),
            format=LogEvaluation.model_json_schema(),
            think=one_shot_think(model),
        )
        raw_output = response["message"]["content"]
        trace = LLMTrace(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_response=raw_output,
        )
        _dur = (__import__("time").monotonic() - _t0) * 1000
        try:
            from utils.debug.capture import record_one_shot as _ros

            _ros(
                "analyze_log_line_with_ai",
                model,
                user_prompt[:500],
                raw_output[:500],
                _dur,
                "success",
                _config.DB_PATH,
            )
        except Exception:  # nosec B110
            pass
        return LogEvaluation.model_validate_json(raw_output), trace
    except Exception as e:
        log.error("triage_inference_failed", error=str(e))
        _dur = (__import__("time").monotonic() - _t0) * 1000
        try:
            from utils.debug.capture import record_one_shot as _ros

            _ros(
                "analyze_log_line_with_ai",
                model,
                user_prompt[:500],
                str(e)[:500],
                _dur,
                "error",
                _config.DB_PATH,
            )
        except Exception:  # nosec B110
            pass
        return LogEvaluation(
            is_actionable=False,
            root_cause_summary="Inference crash",
            confidence_score=0.0,
        ), LLMTrace(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_response="",
        )
    finally:
        if _triage_armed:
            try:
                decrement_active_triage()
                publish_event({"event_type": "triage_done"})
            except Exception:  # nosec B110
                pass


def _repair_issue_knowledge_query(issue: HARepairIssue) -> str:
    """Targeted pre-inject query: domain plus translation key."""
    return f"HA repair issue {issue.domain} {issue.translation_key or ''}".strip()


async def _run_spook_digest_investigation(
    translation_key: str,
    digest_key: str,
    issues: "list[HARepairIssue]",
    notifier: "NotifierProtocol",
    db_path: str,
    llm_client: Optional["LLMClientProtocol"] = None,
    knowledge_store: Optional[Any] = None,
    ha_rest_client: Optional["HARestClientProtocol"] = None,
) -> None:
    """Run one AgentLoop for all Spook issues sharing a translation_key.

    Uses a synthetic HARepairIssue keyed as ``spook/digest:<translation_key>``
    so deduplication works through the standard ``ha_repair_history`` table.
    Subsequent polls skip individual Spook issues via the translation_key dedup
    in ``_check_repair_already_sent`` once the digest row is marked sent.
    """
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_ha_tool_registry
    from utils.ha.read_only_ssh import ReadOnlySSHClient
    from utils.llm.llm_factory import make_llm_client
    from utils.core.prompts import load_agent_prompt

    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = AutonomyGate(AUTONOMY_LEVEL)
    ssh = AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)  # pragma: no cover

    synthetic_issue = HARepairIssue(
        domain="spook",
        issue_id=f"digest:{translation_key}",
        severity=max((i.severity for i in issues), default="warning"),
        issue_key=digest_key,
        translation_key=translation_key or None,
        breaks_in_ha_version=None,
    )

    executor = ToolExecutor(
        ha_ssh_client=ReadOnlySSHClient(ssh),
        gate=gate,
        notifier=notifier,
        ha_rest_client=ha_rest_client,
        knowledge_store=knowledge_store,
        db_path=db_path,
        pending_repair_issue=synthetic_issue,
        post_update_ssh_client=ssh,
    )
    try:
        from utils.ha.ha_environment import load_environment_profile as _lep

        _prof = await asyncio.to_thread(_lep, db_path)
        if _prof is not None:
            executor.set_ha_profile(_prof)
    except Exception:  # nosec B110
        pass

    registry = build_ha_tool_registry()
    system_prompt = load_agent_prompt("repair_issue", "finish_repair_issue")

    issue_lines = "\n".join(f"  - issue_id: {i.issue_id}" for i in issues)
    initial_context = (
        f"Spook repair digest — translation_key: {translation_key or '(none)'}\n"
        f"Issue count: {len(issues)}\n"
        f"Issues:\n{issue_lines}\n\n"
        "Call get_spook_issues to see full details including affected entities.\n"
        "Produce one HITL card covering all issues with this translation_key."
    )

    from utils.agent.supervisor import (
        decrement_active_agent,
        increment_active_agent,
        make_activity_timeline_callback,
        publish_activity_done,
    )

    loop = AgentLoop(
        llm_client=llm,
        tool_executor=executor,
        tool_registry=registry,
        system_prompt=system_prompt,
        terminal_tool_name="finish_repair_issue",
        trigger="repair_poll",
        activity_type="repair_issue",
        db_path=db_path,
        knowledge_store=knowledge_store,
        timeline_callback=make_activity_timeline_callback(
            "repair_issue", trigger=f"Spook digest: {translation_key}"
        ),
        capture_llm=True,
    )
    from utils.knowledge.runbook_signature import sig_repair

    increment_active_agent()
    try:
        result = await loop.run(
            initial_context=initial_context,
            knowledge_query=f"Spook {translation_key} repair issue",
            signature=sig_repair("spook", translation_key),
            subject_key=digest_key,
        )
        publish_activity_done("repair_issue", result.outcome)
        if result.outcome != "success":
            import sqlite3 as _sqlite3_sd
            from utils.hitl.hitl_tracker import mark_investigation_backoff

            with _sqlite3_sd.connect(db_path) as _conn:
                mark_investigation_backoff(_conn, f"repair_issue_backoff:{digest_key}")
    finally:
        decrement_active_agent()


async def _run_repair_issue_investigation(
    issue: HARepairIssue,
    notifier: NotifierProtocol,
    db_path: str,
    llm_client: Optional[LLMClientProtocol] = None,
    knowledge_store: Optional[Any] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    gate: Optional[AutonomyGate] = None,
) -> None:
    """Run an AgentLoop to investigate an HA repair issue and create a HITL card.

    Runs through ``AutonomyGate(AUTONOMY_LEVEL)`` so REST write tools
    (``call_service``, ``reload_integration``) route through the real gate and
    produce HITL cards at lower autonomy levels.  SSH reads use
    ``ReadOnlySSHClient`` (ADR 043); every SSH write raises.  The unwrapped
    ``ssh_client`` is used only for the post-update auto-reboot/restart in
    ``finish_repair_issue``.
    """
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_ha_tool_registry
    from utils.ha.read_only_ssh import ReadOnlySSHClient
    from utils.llm.llm_factory import make_llm_client
    from utils.core.prompts import load_agent_prompt

    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = gate or AutonomyGate(AUTONOMY_LEVEL)
    ssh = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)

    executor = ToolExecutor(
        ha_ssh_client=ReadOnlySSHClient(ssh),
        gate=gate,
        notifier=notifier,
        ha_rest_client=ha_rest_client,
        knowledge_store=knowledge_store,
        db_path=db_path,
        pending_repair_issue=issue,
        post_update_ssh_client=ssh,
    )
    try:
        from utils.ha.ha_environment import load_environment_profile as _lep

        _prof = await asyncio.to_thread(_lep, db_path)
        if _prof is not None:
            executor.set_ha_profile(_prof)
    except Exception:  # nosec B110
        pass

    registry = build_ha_tool_registry()
    system_prompt = load_agent_prompt("repair_issue", "finish_repair_issue")

    initial_context = (
        f"HA repair issue:\n"
        f"Domain: {issue.domain}\n"
        f"Issue ID: {issue.issue_id}\n"
        f"Issue key: {issue.issue_key}\n"
        f"Severity: {issue.severity}\n"
        f"Translation key: {issue.translation_key or '(none)'}\n"
    )
    if issue.breaks_in_ha_version:
        initial_context += f"Breaks in HA version: {issue.breaks_in_ha_version}\n"

    from utils.agent.supervisor import (
        decrement_active_agent,
        increment_active_agent,
        make_activity_timeline_callback,
        publish_activity_done,
    )

    loop = AgentLoop(
        llm_client=llm,
        tool_executor=executor,
        tool_registry=registry,
        system_prompt=system_prompt,
        terminal_tool_name="finish_repair_issue",
        trigger="repair_poll",
        activity_type="repair_issue",
        db_path=db_path,
        knowledge_store=knowledge_store,
        timeline_callback=make_activity_timeline_callback(
            "repair_issue", trigger=f"HA repair: {issue.domain}/{issue.issue_id}"
        ),
        capture_llm=True,
    )
    from utils.knowledge.runbook_signature import sig_repair

    increment_active_agent()
    try:
        result = await loop.run(
            initial_context=initial_context,
            knowledge_query=_repair_issue_knowledge_query(issue),
            signature=sig_repair(issue.domain, issue.translation_key or ""),
            subject_key=issue.issue_key,
        )
        publish_activity_done("repair_issue", result.outcome)
        if result.outcome != "success":
            import sqlite3 as _sqlite3
            from utils.hitl.hitl_tracker import mark_investigation_backoff

            with _sqlite3.connect(db_path) as _conn:
                mark_investigation_backoff(
                    _conn, f"repair_issue_backoff:{issue.issue_key}"
                )
            log.warning(
                "repair_issue_stuck_backoff",
                outcome=result.outcome,
                issue_key=issue.issue_key,
            )
    except Exception as exc:
        log.error(
            "repair_issue_investigation_failed",
            issue_key=issue.issue_key,
            error=str(exc),
        )
    finally:
        decrement_active_agent()


# ==========================================
# STREAMING SSH CONNECTION LAYER
# ==========================================
@async_retry(max_attempts=0, base_delay=SSH_RETRY_BASE_DELAY, exceptions=(OSError,))
async def tail_remote_log_stream(
    source: str = "core",
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    gate: Optional[AutonomyGate] = None,
    notifier: Optional[NotifierProtocol] = None,
) -> None:
    """Streams live HA logs over SSH. source='core' tails ha core; source='supervisor' tails ha supervisor."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    _gate = gate or AutonomyGate(AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    # Resolve once: a per-line make_llm_client() opens a new HTTP pool for every
    # triaged line and exhausts file descriptors during log floods (#769).
    llm_client = llm_client or make_llm_client()
    state = _get_stream_state(source)
    await asyncio.to_thread(_init_sparkline_db, state)
    await asyncio.to_thread(_restore_watermark, state, source)
    state.log_buffer.clear()
    state.entry_ts = None
    log.info("log_stream_start", host=HA_HOST, source=source)

    tail_command = _source_to_command(source)
    _log_buffer = state.log_buffer

    # Explicit iterator so _collect_error_block can read ahead and return a
    # boundary line that the main loop processes on its next iteration.
    _stream_iter = client.stream_lines(tail_command).__aiter__()
    _pending_line: Optional[str] = None

    async def _next_raw() -> Optional[str]:
        nonlocal _pending_line
        if _pending_line is not None:
            raw = _pending_line
            _pending_line = None
            return raw
        try:
            return await _stream_iter.__anext__()
        except StopAsyncIteration:
            return None

    try:
        while True:
            _raw_line = await _next_raw()
            if _raw_line is None:
                break
            clean_line = _raw_line.strip()
            _prev_line = _log_buffer[-1] if _log_buffer else ""
            _log_buffer.append(clean_line)
            _ts = _parse_ha_log_ts(clean_line)
            if _ts is not None:
                state.entry_ts = _ts

            _matched = _is_critical_line(clean_line, _prev_line)
            await asyncio.to_thread(_record_sparkline_line, state, _matched)

            if _matched:
                _line_fp = hashlib.sha256(clean_line.encode()).hexdigest()[:16]
                if not _replay_gate(state, _line_fp):
                    log.info("log_line_replay_skip", source=source, line=clean_line)
                    continue
                if state.entry_ts is not None:
                    await asyncio.to_thread(
                        _save_watermark,
                        source,
                        state.entry_ts,
                        sorted(state.seen_at_watermark),
                    )
                log.warning("log_line_intercepted", line=clean_line)

                # Collect the error's own block (traceback continuation lines up
                # to the next timestamped line) before checking for transient patterns.
                # This prevents false-positive transient skips caused by unrelated
                # network errors that happen to appear in the rolling buffer before
                # the actual error.
                _error_block, _boundary_raw = await _collect_error_block(
                    _stream_iter, clean_line, _log_buffer
                )
                if _boundary_raw is not None:
                    _pending_line = _boundary_raw

                _logger = _extract_logger(clean_line)
                _recurrence_count = await asyncio.to_thread(
                    _check_and_record_recurrence,
                    _logger or "",
                    _strip_log_timestamp(clean_line),
                    time.time(),
                )
                _is_recurring = _recurrence_count >= LOG_RECURRENCE_THRESHOLD

                if _is_recurring and _recurrence_count == LOG_RECURRENCE_THRESHOLD:
                    _exc_class = _extract_exception_class(_error_block)
                    from utils.knowledge.runbook_signature import sig_log

                    log.info(
                        "log_recurrence_threshold_reached",
                        signature=sig_log(_logger or "", _exc_class),
                        count=_recurrence_count,
                    )

                if not _is_recurring and _TRANSIENT_LOG_PATTERNS.search(
                    "\n".join(_error_block)
                ):
                    log.info(
                        "triage_transient_skip",
                        line=clean_line,
                        reason="known-transient network error pattern",
                    )
                    try:  # pragma: no cover
                        from utils.core.timeline import write_timeline_event

                        await asyncio.to_thread(
                            write_timeline_event,
                            "WARNING",
                            state.loop_name,
                            f"Transient external error (auto-resolved): {clean_line[:120]}",
                            {"transient": True, "log_line": clean_line},
                        )
                    except Exception:  # nosec B110
                        pass
                    continue

                # Snapshot mutable values before handing off to the queue consumer.
                _buf_snap = list(_log_buffer)
                _clean_snap = clean_line
                _loop_name = state.loop_name

                async def _triage_coro(
                    _buf: list = _buf_snap,
                    _cl: str = _clean_snap,
                    _ln: str = _loop_name,
                ) -> None:
                    import config as _lm_dbg

                    log.info("triage_start", model=_default_model_for_provider())
                    evaluation, llm_trace = await analyze_log_line_with_ai(
                        _buf, llm_client=llm_client
                    )
                    log.info(
                        "triage_complete",
                        actionable=evaluation.is_actionable,
                        cause=evaluation.root_cause_summary,
                        confidence=evaluation.confidence_score,
                    )
                    if _lm_dbg.DEBUG_LEVEL >= 1:
                        log.debug(
                            "log_triage_result",
                            cause=evaluation.root_cause_summary[:200],
                            confidence=evaluation.confidence_score,
                            actionable=evaluation.is_actionable,
                        )
                    if not (
                        evaluation.is_actionable
                        and evaluation.confidence_score > CONFIDENCE_THRESHOLD
                    ):
                        return
                    try:  # pragma: no cover
                        from utils.core.timeline import write_timeline_event

                        await asyncio.to_thread(
                            write_timeline_event,
                            "ERROR",
                            _ln,
                            evaluation.root_cause_summary,
                            {
                                "confidence": evaluation.confidence_score,
                                "log_line": _cl,
                                "llm_trace": llm_trace.as_dict(),
                            },
                        )
                    except Exception:  # nosec B110
                        pass
                    if not SELF_HEALING_ENABLED:
                        log.info(
                            "self_healing_disabled",
                            cause=evaluation.root_cause_summary,
                        )
                        return
                    if not _debouncer.record():
                        log.info("debounce_suppressed")
                        return
                    try:
                        _rate_limiter.check()
                    except RateLimitExceeded:
                        log.warning("rate_limit_exceeded")
                        return
                    # Persistent dedup: skip if we already sent an approval card for this
                    # error pattern within the cooldown window. Survives daemon restarts
                    # unlike the in-memory _debouncer.
                    _fingerprint = hashlib.sha256(
                        evaluation.root_cause_summary.encode()
                    ).hexdigest()[:16]
                    _now = time.time()
                    _should_send = await asyncio.to_thread(
                        _triage_check_and_record, _fingerprint, _now
                    )
                    if not _should_send:
                        log.info(
                            "log_triage_cooldown",
                            fingerprint=_fingerprint,
                            cause=evaluation.root_cause_summary,
                        )
                        return
                    if not _gate.should_auto_execute(RiskLevel.HIGH):
                        log.info(
                            "autonomy_gate_blocked",
                            cause=evaluation.root_cause_summary,
                        )
                        _suppression_key = f"log_triage:{_fingerprint}"
                        from utils.hitl.hitl_tracker import stable_nid

                        await asyncio.to_thread(
                            _triage_mark_hitl_sent, _fingerprint, _now
                        )
                        await _notifier.send(
                            subject="Pueo: Actionable log event — approval required",
                            body=evaluation.root_cause_summary,
                            payload={
                                "notification_id": stable_nid(_suppression_key),
                                "suppression_key": _suppression_key,
                                "cause": evaluation.root_cause_summary,
                                "confidence": evaluation.confidence_score,
                                "diagnosis": evaluation.model_dump(),
                                "evidence_raw": {"log_buffer_snapshot": list(_buf)},
                                "llm_trace": llm_trace.as_dict(),
                            },
                        )
                        return
                    log.warning("repair_triggered")
                    await asyncio.to_thread(_triage_mark_hitl_sent, _fingerprint, _now)
                    from utils.agent.work_queue import (
                        PRIORITY_CRITICAL,
                        WorkItem,
                        get_work_queue_or_none,
                    )

                    _wq = get_work_queue_or_none()
                    if _wq is not None:
                        await _wq.submit(
                            WorkItem(
                                priority=PRIORITY_CRITICAL,
                                activity_type="ha_repair",
                                description=f"Repair: {evaluation.root_cause_summary[:80]}",
                                dedup_key=f"ha_repair:{_fingerprint}",
                                suppress_while_running=frozenset(),
                                coro_factory=trigger_remediation_pipeline,
                            )
                        )
                    else:
                        asyncio.create_task(trigger_remediation_pipeline())

                from utils.agent.work_queue import (
                    PRIORITY_HIGH,
                    WorkItem,
                    get_work_queue_or_none,
                )

                _wq = get_work_queue_or_none()
                if _wq is not None:
                    await _wq.submit(
                        WorkItem(
                            priority=PRIORITY_HIGH,
                            activity_type="triage",
                            description=f"Triage: {_clean_snap[:80]}",
                            dedup_key=f"triage:{_line_fp}",
                            suppress_while_running=frozenset(),
                            coro_factory=_triage_coro,
                        )
                    )
                else:
                    await _triage_coro()

    except Exception as e:
        if isinstance(e, OSError) and e.errno in _TRANSIENT_SSH_ERRNOS:
            log.warning("log_stream_reset", error=str(e))
        else:
            log.error("log_stream_failed", error=str(e))
        log.info("log_stream_reconnect")
        raise


def _component_from_update_key(suppression_key: str) -> str:
    """Derive a readable component name from an update suppression key.

    "update:update.gazpar2mqtt_update" → "gazpar2mqtt".
    """
    name = suppression_key.removeprefix("update:").removeprefix("update.")
    return name.removesuffix("_update") or name


def _external_update_message(
    component: str, from_version: Optional[str], to_version: Optional[str]
) -> str:
    """Build the external-resolution timeline text from whichever parts are known."""
    if from_version and to_version:
        return f"{component} updated {from_version} → {to_version} outside Pueo"
    if to_version:
        return f"{component} updated to {to_version} outside Pueo"
    return f"{component} update cleared outside Pueo"


# How long after Pueo auto-applies an update a sibling entity's disappearance is
# attributed to Pueo rather than to an external action.  Two full fallback poll
# intervals (2 × 60 min) gives comfortable headroom for the two-poll confirmation
# and the event-driven wake-up debounce.
_SIBLING_AUTO_APPLY_WINDOW_SECONDS = 7200.0


def _find_recent_pueo_auto_apply(component: str) -> Optional[str]:
    """Return the card_key of a recently Pueo-auto-applied update with a related component.

    "Related" means either component name is a substring of the other, which
    handles cases like "spook" / "spook_your_homie" being the same package.
    Returns None when no match is found or on any DB error.
    """
    cutoff = time.time() - _SIBLING_AUTO_APPLY_WINDOW_SECONDS
    try:
        with sqlite3.connect(DB_PATH) as _conn:
            rows = _conn.execute(
                "SELECT card_key, last_action_at FROM hitl_suppression"
                " WHERE card_key LIKE 'update:%'"
                "   AND last_action = 'auto_applied'"
                "   AND last_action_at > ?",
                (cutoff,),
            ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row_key, _ in rows:
        row_component = _component_from_update_key(row_key)
        if component in row_component or row_component in component:
            return row_key
    return None


def _resolve_externally_applied_update(
    suppression_key: str, watch_dir: str, update: Optional[UpdateStatus] = None
) -> None:
    """Retire a pending update approval card that HA resolved without Pueo's involvement.

    Details come from the approval-card JSON when present; otherwise from the
    current poll's UpdateStatus, then the entity id, so the message never shows
    "unknown" (#843).

    If Pueo recently auto-applied an update for a related component (a sibling
    entity cleared within ``_SIBLING_AUTO_APPLY_WINDOW_SECONDS``), the resolution
    is attributed to Pueo rather than to an external action.
    """
    # Suppress false "external" events while Pueo is actively executing this update.
    entity_id_from_key = suppression_key.removeprefix("update:")
    try:
        from agents.ha_update_manager import is_entity_executing_update

        if is_entity_executing_update(entity_id_from_key):
            log.debug(
                "update_resolved_externally_suppressed",
                entity_id=entity_id_from_key,
                reason="pueo_executing",
            )
            return
    except Exception:  # nosec B110
        pass

    from utils.hitl.hitl_tracker import (
        mark_card_auto_applied,
        mark_card_resolved,
        stable_nid,
    )
    from utils.hitl.card_types import CARD_TYPE_UPDATE

    nid = stable_nid(suppression_key)
    component: Optional[str] = None
    from_version: Optional[str] = None
    to_version: Optional[str] = None

    card_path = Path(watch_dir) / f"{nid}.json"
    try:
        if card_path.exists():
            data = json.loads(card_path.read_text())
            payload = data.get("payload", data)
            component = payload.get("component")
            from_version = payload.get("installed_version")
            to_version = payload.get("latest_version")
    except Exception:  # nosec B110
        pass

    if update is not None:
        component = component or update.component
        to_version = to_version or update.installed_version
    component = component or _component_from_update_key(suppression_key)

    # Check if a sibling update entity was recently auto-applied by Pueo.
    _sibling_key = _find_recent_pueo_auto_apply(component)
    _is_pueo_sibling = _sibling_key is not None

    entity_id = entity_id_from_key
    with sqlite3.connect(DB_PATH) as _conn:
        if _is_pueo_sibling:
            # Record as Pueo's action: not pending, not external.
            mark_card_auto_applied(
                _conn,
                suppression_key,
                CARD_TYPE_UPDATE,
                f"Sibling of Pueo-applied {_sibling_key}: {component} update cleared",
            )
        else:
            mark_card_resolved(_conn, suppression_key)
        _conn.execute(
            "UPDATE hitl_suppression SET resolved_at = ?"
            " WHERE card_key LIKE ? AND resolved_at IS NULL",
            (time.time(), f"update_analyzed:{entity_id}:%"),
        )

    try:
        from utils.core.timeline import write_timeline_event

        if _is_pueo_sibling:
            write_timeline_event(
                "INFO",
                "update_check",
                f"{component} update cleared — attributed to Pueo's recent"
                f" auto-apply of sibling {_sibling_key}",
                {
                    "component": component,
                    "from_version": from_version,
                    "to_version": to_version,
                    "resolution": "pueo_sibling",
                    "sibling_key": _sibling_key,
                },
            )
        else:
            write_timeline_event(
                "INFO",
                "update_check",
                f"{_external_update_message(component, from_version, to_version)}"
                " (HA auto-update or manual action in HA UI)",
                {
                    "component": component,
                    "from_version": from_version,
                    "to_version": to_version,
                    "resolution": "external",
                    "explanation": (
                        f"{_external_update_message(component, from_version, to_version)}"
                        " without Pueo approval."
                    ),
                },
            )
    except Exception:  # nosec B110
        pass

    approved_path = Path(watch_dir) / f"{nid}.approved"
    if card_path.exists() and not approved_path.exists():
        try:
            approved_path.write_text("")
        except Exception:  # nosec B110
            pass

    if _is_pueo_sibling:
        log.info(
            "update_resolved_as_pueo_sibling",
            suppression_key=suppression_key,
            component=component,
            sibling_key=_sibling_key,
        )
    else:
        log.info(
            "update_resolved_externally",
            suppression_key=suppression_key,
            component=component,
            from_version=from_version,
            to_version=to_version,
        )


async def poll_for_updates(
    ha_rest_client: Optional[HARestClientProtocol] = None,
    notifier: Optional[NotifierProtocol] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    knowledge_store: Optional[Any] = None,
    event_subscriber: Optional[HAEventSubscriberProtocol] = None,
) -> None:
    """Periodically checks for available HA updates and fires update approval cards."""
    interval = HA_UPDATE_CHECK_INTERVAL_HOURS * 3600
    _client: HARestClientProtocol = ha_rest_client or HARestClient(
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    _update_gone: dict[str, float] = (
        {}
    )  # suppression_key → time.monotonic() of first absence
    _timeline_written: set[str] = (
        set()
    )  # "{entity_id}:{version}" already logged this run

    from .ha_update_manager import reconcile_stale_approved_cards

    _reconciled = reconcile_stale_approved_cards()
    if _reconciled:
        log.info("poll_for_updates_reconciled", count=_reconciled)

    while True:
        try:
            updates = await get_update_status(_client)
        except Exception as exc:
            log.warning("update_poll_failed", error=str(exc))
            await asyncio.sleep(interval)
            continue

        for u in updates:
            suppression_key = f"update:{u.entity_id}"
            if not u.state_known:
                # unavailable/unknown during an HA restart or HACS reload says nothing
                # about the update; resolving here would force a re-analysis (#976).
                log.debug("update_poll_skip_unknown_state", entity_id=u.entity_id)
                continue
            if u.update_available:
                _update_gone.pop(
                    suppression_key, None
                )  # update came back — reset timer

                # Skip updates that are already being installed — HA emits rapid
                # attribute-only changes (in_progress, update_percentage) during an
                # install; these would create duplicate timeline entries.
                if u.in_progress:
                    log.debug(
                        "update_poll_skip_in_progress",
                        component=u.component,
                        version=u.latest_version,
                    )
                    continue

                _should_send = await asyncio.to_thread(
                    _update_check_should_send, suppression_key
                )
                if not _should_send:
                    continue
                if not await asyncio.to_thread(
                    _update_check_should_send,
                    f"update_analysis_backoff:{u.entity_id}",
                ):
                    log.info(
                        "update_analysis_backoff_active",
                        component=u.component,
                        version=u.latest_version,
                    )
                    continue
                # Skip if already analyzed for this specific version with no card needed.
                analyzed_key = f"update_analyzed:{u.entity_id}:{u.latest_version}"
                if await asyncio.to_thread(_update_already_analyzed, analyzed_key):
                    continue

                _analysis_accepted = True
                if HA_UPDATE_NOTIFY_ON_AVAILABLE:
                    from .ha_update_manager import _run_update_analysis

                    _analysis_accepted = await _run_update_analysis(
                        u,
                        ssh_client=ssh_client,
                        notifier=_notifier,
                        knowledge_store=knowledge_store,
                        ha_rest_client=_client,
                    )
                    if not _analysis_accepted:
                        log.debug(
                            "update_analysis_already_queued",
                            component=u.component,
                            version=u.latest_version,
                        )
                        continue

                log.info(
                    "update_available",
                    component=u.component,
                    installed=u.installed_version,
                    latest=u.latest_version,
                )
                _tl_key = f"{u.entity_id}:{u.latest_version}"
                if _tl_key not in _timeline_written:
                    try:  # pragma: no cover
                        from utils.core.timeline import write_timeline_event

                        await asyncio.to_thread(
                            write_timeline_event,
                            "INFO",
                            "update_check",
                            f"Update available: {u.component}"
                            f" {u.installed_version} → {u.latest_version}",
                            {
                                "component": u.component,
                                "installed_version": u.installed_version,
                                "latest_version": u.latest_version,
                                "release_url": u.release_url,
                            },
                        )
                    except Exception:  # nosec B110
                        pass
                    _timeline_written.add(_tl_key)
            elif not u.update_available:
                _is_pending = await asyncio.to_thread(
                    _update_check_pending, suppression_key
                )
                if not _is_pending:
                    _update_gone.pop(suppression_key, None)
                    await asyncio.to_thread(_update_resolve_card, suppression_key)
                    _closed = await asyncio.to_thread(
                        _close_installed_update_card, suppression_key, NOTIFY_WATCH_DIR
                    )
                    if _closed:
                        try:
                            from utils.core.timeline import write_timeline_event

                            await asyncio.to_thread(
                                write_timeline_event,
                                "INFO",
                                "update_check",
                                f"{u.component} update to"
                                f" {u.installed_version} confirmed installed"
                                f" — completing interrupted approval",
                                {
                                    "component": u.component,
                                    "installed_version": u.installed_version,
                                    "resolution": "interrupted_approval_closed",
                                },
                            )
                        except Exception:  # nosec B110
                            pass
                        try:
                            from agents.ha_agent_advanced import resolve_session_outcome
                            from agents.ha_update_manager import (
                                _enqueue_distillation_for_subject,
                            )

                            await asyncio.to_thread(
                                resolve_session_outcome,
                                u.entity_id,
                                "success",
                                "system",
                            )
                            await _enqueue_distillation_for_subject(
                                u.entity_id, "success"
                            )
                        except Exception:  # nosec B110
                            pass
                if _is_pending:
                    if suppression_key not in _update_gone:
                        _update_gone[suppression_key] = time.monotonic()
                        log.info(
                            "update_gone_first_detection",
                            suppression_key=suppression_key,
                        )
                    else:
                        await asyncio.to_thread(
                            _resolve_externally_applied_update,
                            suppression_key,
                            NOTIFY_WATCH_DIR,
                            u,
                        )
                        _update_gone.pop(suppression_key, None)

        _n_avail = sum(1 for u in updates if u.update_available)
        _update_outcome = (
            "No updates" if _n_avail == 0 else f"{_n_avail} update(s) available"
        )
        # Reconcile: pending update cards for entities entirely absent from HA response.
        seen_keys = {f"update:{u.entity_id}" for u in updates}
        _absent_pending = await asyncio.to_thread(
            _update_sweep_absent_pending, seen_keys
        )
        for (_abs_key,) in _absent_pending:
            if _abs_key in seen_keys:
                continue  # handled in per-entity loop above
            if _abs_key not in _update_gone:
                _update_gone[_abs_key] = time.monotonic()
            else:
                await asyncio.to_thread(
                    _resolve_externally_applied_update, _abs_key, NOTIFY_WATCH_DIR
                )
                _update_gone.pop(_abs_key, None)

        try:
            from utils.agent.supervisor import get_supervisor_instance as _get_sv

            _sv_inst = _get_sv()
            if _sv_inst is not None:
                _sv_inst.touch("update_check", outcome=_update_outcome)
        except Exception:  # nosec B110
            pass
        from utils.agent.supervisor import supervised_sleep as _sup_sleep

        await _sup_sleep(
            "update_check", _poll_sleep_seconds(interval, event_subscriber)
        )


async def poll_for_notifications(
    notifier: Optional[NotifierProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    ha_ws_client: Optional[HAWebSocketClientProtocol] = None,
    netalertx_client: Optional[NetAlertXClientProtocol] = None,
    db_path: str = DB_PATH,
    knowledge_store: Optional[Any] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
    event_subscriber: Optional[HAEventSubscriberProtocol] = None,
) -> None:
    """Periodically checks for new HA persistent notifications and fires approval alerts."""
    from .ha_notification_manager import (
        _run_notification_investigation,
        classify_notification,
        record_notification_seen,
    )
    from netalertx.api_client import NetAlertXAPIClient
    from utils.ha.ha_ws_client import HAWebSocketClient

    interval = HA_NOTIFICATION_POLL_INTERVAL_MINUTES * 60
    _ws: HAWebSocketClientProtocol = ha_ws_client or HAWebSocketClient(
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )  # pragma: no cover
    _rest: HARestClientProtocol = ha_rest_client or HARestClient(  # pragma: no cover
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    _llm: LLMClientProtocol = llm_client or make_llm_client()  # pragma: no cover
    _nax: NetAlertXClientProtocol = (
        netalertx_client
        or NetAlertXAPIClient(  # pragma: no cover
            f"http://{NETALERTX_HOST}:{NETALERTX_API_PORT}", NETALERTX_API_TOKEN
        )
    )

    backoff: int = 0  # seconds; 0 means use normal interval
    while True:
        from utils.agent.supervisor import supervised_sleep as _sup_sleep_n

        await _sup_sleep_n(
            "notification_poll",
            _poll_sleep_seconds(interval, event_subscriber, backoff),
        )
        try:
            notifications = await _ws.get_persistent_notifications()
            backoff = 0  # reset on success
        except Exception as exc:
            log.warning("notification_poll_failed", error=str(exc))
            backoff = min(backoff * 2 if backoff else 30, 120)
            continue

        _new_notif_count = 0
        for notif in notifications:
            nid: str = notif.get("notification_id", "")
            if not nid:
                log.warning("notification_id_missing", notif_keys=list(notif.keys()))
                continue
            title: Optional[str] = notif.get("title")
            message: str = notif.get("message", "")
            ha_created_at: Optional[float] = None
            created_at_str: Optional[str] = notif.get("created_at")
            if created_at_str:
                try:
                    ha_created_at = datetime.datetime.fromisoformat(
                        created_at_str
                    ).timestamp()
                except Exception:  # nosec B110
                    log.warning(
                        "notification_created_at_parse_failed", raw=created_at_str
                    )

            category, severity = classify_notification(nid)
            record_notification_seen(
                nid, category, severity, db_path=db_path, ha_created_at=ha_created_at
            )

            def _check_notif_should_send(_nid: str) -> bool:
                with sqlite3.connect(db_path) as _conn:
                    _row = _conn.execute(
                        "SELECT hitl_sent_at FROM notification_history"
                        " WHERE notification_id = ?",
                        (_nid,),
                    ).fetchone()
                return _row is not None and _row[0] is None

            should_send = await asyncio.to_thread(_check_notif_should_send, nid)

            if should_send:
                # Capture loop-local variables for the async closure.
                _nid = nid
                _title = title
                _message = message
                _ha_created_at = ha_created_at
                _notifier_ref = _notifier
                _nax_ref = _nax
                _ws_ref = _ws
                _rest_ref = _rest
                _llm_ref = _llm
                _db = db_path

                async def _run_investigation(
                    nid: str = _nid,
                    title: "Optional[str]" = _title,
                    message: str = _message,
                    ha_created_at: "Optional[float]" = _ha_created_at,
                ) -> None:
                    await _run_notification_investigation(
                        notification_id=nid,
                        title=title,
                        message=message,
                        ha_created_at=ha_created_at,
                        db_path=_db,
                        notifier=_notifier_ref,
                        llm_client=_llm_ref,
                        netalertx_client=_nax_ref,
                        ws_client=_ws_ref,
                        knowledge_store=knowledge_store,
                        ha_rest_client=_rest_ref,
                    )

                from utils.agent.work_queue import (
                    PRIORITY_HIGH,
                    WorkItem,
                    get_work_queue_or_none,
                )

                _wq = get_work_queue_or_none()
                if _wq is not None:
                    await _wq.submit(
                        WorkItem(
                            priority=PRIORITY_HIGH,
                            activity_type="notification",
                            description=f"Notification triage: {nid}",
                            dedup_key=f"notification:{nid}",
                            suppress_while_running=frozenset(),
                            coro_factory=_run_investigation,
                        )
                    )
                else:
                    await _run_investigation()
                _new_notif_count += 1

        _notif_outcome = (
            "No new notifications"
            if _new_notif_count == 0
            else f"{_new_notif_count} new"
        )
        try:
            from utils.agent.supervisor import get_supervisor_instance as _get_sv

            _sv_inst = _get_sv()
            if _sv_inst is not None:
                _sv_inst.touch("notification_poll", outcome=_notif_outcome)
        except Exception:  # nosec B110
            pass


_HACS_RESTART_REQUIRED_RE = re.compile(r"hacs/restart_required_(\d+)")


def _hacs_repo_id_from_issue(issue: HARepairIssue) -> str:
    """Extract the numeric HACS repo id from a hacs/restart_required_<id>_* issue key."""
    if issue.domain != "hacs":
        return ""
    m = _HACS_RESTART_REQUIRED_RE.match(issue.issue_key)
    return m.group(1) if m else ""


async def poll_for_repairs(
    ha_ws_client: Optional[HAWebSocketClientProtocol] = None,
    notifier: Optional[NotifierProtocol] = None,
    db_path: str = DB_PATH,
    llm_client: Optional[LLMClientProtocol] = None,
    knowledge_store: Optional[Any] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
    event_subscriber: Optional[HAEventSubscriberProtocol] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
) -> None:
    """Periodically polls HA repairs via WebSocket and fires approval cards for new issues."""
    from .ha_agent_advanced import (
        mark_repair_resolved,
        record_repair_seen,
        resolve_session_outcome,
    )
    from utils.ha.ha_rest_client import get_ha_repair_issues
    from utils.ha.ha_ws_client import HAWebSocketClient

    interval = HA_REPAIR_POLL_INTERVAL_MINUTES * 60
    _client: HAWebSocketClientProtocol = ha_ws_client or HAWebSocketClient(
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )
    _rest: HARestClientProtocol = ha_rest_client or HARestClient(  # pragma: no cover
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    _ssh: SSHClientProtocol = ssh_client or AsyncSSHClient(  # pragma: no cover
        HA_HOST, HA_USER, SSH_KEY_PATH
    )

    repair_backoff: int = 0  # seconds; 0 means use normal interval
    while True:
        from utils.agent.supervisor import supervised_sleep as _sup_sleep_r

        await _sup_sleep_r(
            "repair_poll",
            _poll_sleep_seconds(interval, event_subscriber, repair_backoff),
        )
        try:
            issues = await get_ha_repair_issues(_client)
            repair_backoff = 0  # reset on success
        except Exception as exc:
            log.warning("repair_poll_failed", error=str(exc))
            repair_backoff = min(repair_backoff * 2 if repair_backoff else 30, 120)
            continue

        active_keys: set[str] = set()
        _spook_unsent_by_tkey: dict[str, list] = {}
        for issue in issues:
            active_keys.add(issue.issue_key)
            record_repair_seen(issue.issue_key, translation_key=issue.translation_key)
            import sqlite3 as _sqlite3

            def _check_repair_already_sent(_key: str, _tkey: str) -> bool:
                with _sqlite3.connect(db_path) as _conn:
                    _row = _conn.execute(
                        "SELECT hitl_sent_at FROM ha_repair_history WHERE issue_key = ?",
                        (_key,),
                    ).fetchone()
                    # Also check by translation_key: HA assigns new UUIDs after restart, so
                    # the same logical repair issue (e.g. "reboot_required") would bypass
                    # the issue_key check and generate a duplicate card.
                    _tkey_row = None
                    if _tkey:
                        _tkey_row = _conn.execute(
                            "SELECT hitl_sent_at FROM ha_repair_history"
                            " WHERE translation_key = ? AND hitl_sent_at IS NOT NULL"
                            " AND resolved_at IS NULL AND issue_key != ?",
                            (_tkey, _key),
                        ).fetchone()
                return (
                    _row is not None and _row[0] is not None
                ) or _tkey_row is not None

            already_sent = await asyncio.to_thread(
                _check_repair_already_sent,
                issue.issue_key,
                issue.translation_key or "",
            )

            if not already_sent:
                # ── Spook digest routing ──────────────────────────────────────
                # Divert all new Spook issues into a per-translation_key digest
                # dispatched after the main loop; one investigation per tkey.
                if issue.domain == "spook":
                    _tkey_s = issue.translation_key or ""
                    _spook_unsent_by_tkey.setdefault(_tkey_s, []).append(issue)
                    continue
                # ── end Spook digest routing ──────────────────────────────────

                _backoff_key = f"repair_issue_backoff:{issue.issue_key}"
                from utils.hitl.hitl_tracker import should_send_card as _ssc

                with _sqlite3.connect(db_path) as _conn:
                    if not _ssc(_conn, _backoff_key):
                        continue

                # ── HACS restart_required short-circuit ───────────────────────
                # When a hacs/restart_required_<repo_id>_* issue appears and Pueo
                # has a fresh DB-persisted pending restart flag (written by the
                # just-completed addon update), skip the LLM entirely.
                _hacs_repo_id = _hacs_repo_id_from_issue(issue)
                if _hacs_repo_id:
                    from agents.ha_update_manager import (
                        find_any_post_update_pending,
                        clear_post_update_pending,
                        execute_ha_core_restart,
                    )

                    _pending_slug = await asyncio.to_thread(
                        find_any_post_update_pending, "restart", db_path
                    )
                    if _pending_slug is not None:
                        # Consume the flag before taking action.
                        await asyncio.to_thread(
                            clear_post_update_pending, _pending_slug, db_path
                        )

                        # Enrich with repo info from profile if available.
                        _repo_label = _hacs_repo_id
                        try:
                            from utils.ha.ha_environment import load_environment_profile

                            _prof = await asyncio.to_thread(
                                load_environment_profile, db_path
                            )
                            if _prof and _hacs_repo_id in (_prof.hacs_repo_map or {}):
                                _full_name, _domain, _loaded = _prof.hacs_repo_map[
                                    _hacs_repo_id
                                ]
                                _load_note = (
                                    "loaded" if _loaded else "downloaded but not set up"
                                )
                                _repo_label = (
                                    f"{_full_name} (domain: {_domain}, {_load_note})"
                                )
                        except Exception:  # nosec B110
                            pass

                        _gate = AutonomyGate(AUTONOMY_LEVEL)
                        _should_auto = _gate.should_auto_execute(RiskLevel.MEDIUM)
                        log.info(
                            "hacs_restart_required_short_circuit",
                            issue_key=issue.issue_key,
                            repo_id=_hacs_repo_id,
                            repo_label=_repo_label,
                            pending_slug=_pending_slug,
                            auto_execute=_should_auto,
                        )

                        if _should_auto:
                            from utils.agent.work_queue import (
                                PRIORITY_HIGH,
                                WorkItem,
                                get_work_queue_or_none,
                            )
                            from agents.ha_agent_advanced import mark_repair_hitl_sent

                            _issue_sc = issue
                            _ssh_sc = _ssh
                            _notifier_sc = _notifier
                            _repo_label_sc = _repo_label
                            _db_sc = db_path

                            async def _do_auto_restart(
                                _iss: HARepairIssue = _issue_sc,
                                _s: SSHClientProtocol = _ssh_sc,
                                _n: NotifierProtocol = _notifier_sc,
                                _rl: str = _repo_label_sc,
                                _dp: str = _db_sc,
                            ) -> None:
                                from agents.ha_agent_advanced import (
                                    mark_repair_resolved,
                                )

                                mark_repair_hitl_sent(_iss.issue_key)
                                success = await execute_ha_core_restart(
                                    _s, _n, skip_backup=True
                                )
                                if success:
                                    mark_repair_resolved(_iss.issue_key)
                                from utils.core.timeline import write_timeline_event

                                write_timeline_event(
                                    "INFO" if success else "WARNING",
                                    "repair_poll",
                                    f"HACS restart_required ({_rl}): "
                                    + (
                                        "auto-restart succeeded"
                                        if success
                                        else "auto-restart timed out"
                                    ),
                                    {
                                        "issue_key": _iss.issue_key,
                                        "repo_id": _hacs_repo_id,
                                        "repo_label": _rl,
                                        "auto_executed": True,
                                        "success": success,
                                    },
                                )

                            _wq = get_work_queue_or_none()
                            if _wq is not None:
                                await _wq.submit(
                                    WorkItem(
                                        priority=PRIORITY_HIGH,
                                        activity_type="restart_execution",
                                        description=f"Auto-restart: HACS {_hacs_repo_id}",
                                        dedup_key=f"hacs_restart:{issue.issue_key}",
                                        suppress_while_running=frozenset(),
                                        coro_factory=_do_auto_restart,
                                    )
                                )
                            else:
                                await _do_auto_restart()
                        else:
                            # Send a restart card (no LLM).
                            from utils.hitl.card_types import CARD_TYPE_HA_REPAIR
                            from utils.hitl.hitl_tracker import stable_nid
                            from agents.ha_agent_advanced import mark_repair_hitl_sent

                            _repair_sup_key = (
                                f"ha_repair:{issue.translation_key or issue.issue_key}"
                            )
                            _title = "HA Core restart required — HACS update applied"
                            _body = (
                                f"Repository: {_repo_label}\n"
                                "Pueo applied a HACS integration update that requires"
                                " an HA Core restart to take effect.\n"
                                "Approve to restart now."
                            )
                            _payload: dict = {
                                "notification_id": stable_nid(_repair_sup_key),
                                "card_type": CARD_TYPE_HA_REPAIR,
                                "suppression_key": _repair_sup_key,
                                "action": "restart",
                                "domain": issue.domain,
                                "issue_id": issue.issue_id,
                                "issue_key": issue.issue_key,
                                "severity": issue.severity.upper(),
                                "title": _title,
                                "body": _body,
                                "translation_key": issue.translation_key,
                                "repo_id": _hacs_repo_id,
                                "repo_label": _repo_label,
                            }
                            mark_repair_hitl_sent(issue.issue_key)
                            await _notifier.send(
                                subject=_title, body=_body, payload=_payload
                            )
                            try:
                                from utils.core.timeline import write_timeline_event

                                write_timeline_event(
                                    "INFO",
                                    "repair_poll",
                                    f"HACS restart_required ({_repo_label}): restart card sent",
                                    {
                                        "issue_key": issue.issue_key,
                                        "repo_id": _hacs_repo_id,
                                        "repo_label": _repo_label,
                                        "auto_executed": False,
                                    },
                                )
                            except Exception:  # nosec B110
                                pass

                        continue  # skip the normal LLM investigation for this issue

                    # No pending flag — proceed with normal investigation but
                    # enrich initial_context with HACS repo info.
                    _repo_label_enrich = _hacs_repo_id
                    try:
                        from utils.ha.ha_environment import load_environment_profile

                        _prof_e = await asyncio.to_thread(
                            load_environment_profile, db_path
                        )
                        if _prof_e and _hacs_repo_id in (_prof_e.hacs_repo_map or {}):
                            _fn_e, _dom_e, _loaded_e = _prof_e.hacs_repo_map[
                                _hacs_repo_id
                            ]
                            _note_e = (
                                "loaded in HA"
                                if _loaded_e
                                else "downloaded via HACS but not set up"
                            )
                            _repo_label_enrich = (
                                f"{_fn_e} (domain: {_dom_e}, {_note_e})"
                            )
                    except Exception:  # nosec B110
                        pass

                    log.info(
                        "hacs_restart_required_investigation",
                        issue_key=issue.issue_key,
                        repo_id=_hacs_repo_id,
                        repo_label=_repo_label_enrich,
                        pending=False,
                    )
                # ── end HACS restart_required short-circuit ───────────────────

                _issue_snap = issue
                _notifier_snap = _notifier
                _llm_snap = llm_client
                _db_snap = db_path
                _rest_snap = _rest

                async def _run_investigation(
                    _issue: HARepairIssue = _issue_snap,
                    _notifier_ref: NotifierProtocol = _notifier_snap,
                    _llm_ref: Optional[LLMClientProtocol] = _llm_snap,
                    _db_ref: str = _db_snap,
                    _rest_ref: HARestClientProtocol = _rest_snap,
                ) -> None:
                    await _run_repair_issue_investigation(
                        issue=_issue,
                        notifier=_notifier_ref,
                        db_path=_db_ref,
                        llm_client=_llm_ref,
                        knowledge_store=knowledge_store,
                        ha_rest_client=_rest_ref,
                    )

                from utils.agent.work_queue import (
                    PRIORITY_NORMAL,
                    WorkItem,
                    get_work_queue_or_none,
                )

                _wq = get_work_queue_or_none()
                if _wq is not None:
                    await _wq.submit(
                        WorkItem(
                            priority=PRIORITY_NORMAL,
                            activity_type="repair_issue",
                            description=(
                                f"Repair issue investigation: {issue.domain}/{issue.issue_id}"
                            ),
                            dedup_key=f"repair_issue:{issue.issue_key}",
                            suppress_while_running=frozenset(),
                            coro_factory=_run_investigation,
                        )
                    )
                else:
                    await _run_investigation()

        # ── Spook digest dispatch ─────────────────────────────────────────────
        # For each translation_key that has at least one new Spook issue, dispatch
        # a single digest investigation (or skip if a digest was already sent).
        for _tkey_d, _tkey_d_issues in _spook_unsent_by_tkey.items():
            _digest_key_d = f"spook/digest:{_tkey_d}" if _tkey_d else "spook/digest:"
            active_keys.add(_digest_key_d)

            def _check_digest_sent(_dk: str = _digest_key_d) -> bool:
                with sqlite3.connect(db_path) as _conn:
                    _row = _conn.execute(
                        "SELECT hitl_sent_at FROM ha_repair_history"
                        " WHERE issue_key = ? AND hitl_sent_at IS NOT NULL"
                        " AND resolved_at IS NULL",
                        (_dk,),
                    ).fetchone()
                    return _row is not None

            if await asyncio.to_thread(_check_digest_sent):
                continue

            from utils.hitl.hitl_tracker import should_send_card as _ssc_d

            with sqlite3.connect(db_path) as _conn:
                if not _ssc_d(_conn, f"repair_issue_backoff:{_digest_key_d}"):
                    continue

            await asyncio.to_thread(
                record_repair_seen, _digest_key_d, translation_key=_tkey_d or None
            )

            _tkey_snap_d = _tkey_d
            _dkey_snap_d = _digest_key_d
            _issues_snap_d = list(_tkey_d_issues)
            _notifier_snap_d = _notifier
            _llm_snap_d = llm_client
            _db_snap_d = db_path
            _rest_snap_d = _rest

            async def _run_spook_digest_coro(
                _tk: str = _tkey_snap_d,
                _dk: str = _dkey_snap_d,
                _is: "list[HARepairIssue]" = _issues_snap_d,
                _n: NotifierProtocol = _notifier_snap_d,
                _l: Optional[LLMClientProtocol] = _llm_snap_d,
                _db: str = _db_snap_d,
                _r: HARestClientProtocol = _rest_snap_d,
            ) -> None:
                await _run_spook_digest_investigation(
                    translation_key=_tk,
                    digest_key=_dk,
                    issues=_is,
                    notifier=_n,
                    db_path=_db,
                    llm_client=_l,
                    knowledge_store=knowledge_store,
                    ha_rest_client=_r,
                )

            from utils.agent.work_queue import (
                PRIORITY_NORMAL as _PRIO_D,
                WorkItem as _WI_D,
                get_work_queue_or_none as _gwq_d,
            )

            _wq_d = _gwq_d()
            log.info(
                "spook_digest_dispatched",
                translation_key=_tkey_d,
                issue_count=len(_tkey_d_issues),
                digest_key=_digest_key_d,
            )
            if _wq_d is not None:
                await _wq_d.submit(
                    _WI_D(
                        priority=_PRIO_D,
                        activity_type="repair_issue",
                        description=(
                            f"Spook digest: {_tkey_d or 'unknown'}"
                            f" ({len(_tkey_d_issues)} issue(s))"
                        ),
                        dedup_key=f"repair_issue:{_digest_key_d}",
                        suppress_while_running=frozenset(),
                        coro_factory=_run_spook_digest_coro,
                    )
                )
            else:
                await _run_spook_digest_coro()
        # ── end Spook digest dispatch ─────────────────────────────────────────

        # Reconcile: mark resolved any previously-sent repairs no longer in HA's list.
        import sqlite3 as _sqlite3

        def _fetch_open_repair_rows() -> list:
            with _sqlite3.connect(db_path) as _conn:
                return _conn.execute(
                    "SELECT issue_key, translation_key FROM ha_repair_history"
                    " WHERE hitl_sent_at IS NOT NULL AND resolved_at IS NULL",
                ).fetchall()

        open_rows = await asyncio.to_thread(_fetch_open_repair_rows)
        for issue_key, translation_key in open_rows:
            if issue_key not in active_keys:
                mark_repair_resolved(issue_key)
                log.info("repair_resolved", issue_key=issue_key)
                # Resolve session outcomes and runbook_usage rows by issue_key (subject_key).
                # This fixes the historic bug where partition(":") failed on keys using "/".
                try:
                    await asyncio.to_thread(
                        resolve_session_outcome, issue_key, "success", "system"
                    )
                except Exception:  # nosec B110
                    pass
                try:
                    from utils.knowledge.runbook_distiller import (
                        enqueue_distillation_after_verification,
                        _fetch_seed_text_for_signature,
                    )
                    import config as _cfg
                    from utils.agent.supervisor import get_supervisor_instance

                    _sv = get_supervisor_instance()
                    _ks = getattr(
                        getattr(_sv, "_tool_executor", None), "_knowledge_store", None
                    )
                    if _ks is not None:
                        import sqlite3 as _sql

                        _ep_ids = []
                        with _sql.connect(_cfg.DB_PATH) as _conn:
                            _rows = _conn.execute(
                                "SELECT episode_id FROM session_outcomes"
                                " WHERE subject_key = ?"
                                "   AND verification = 'success'",
                                (issue_key,),
                            ).fetchall()
                            _ep_ids = [r[0] for r in _rows if r[0]]
                        for _ep_id in _ep_ids:
                            await enqueue_distillation_after_verification(
                                episode_id=_ep_id,
                                verification="success",
                                correction=None,
                                db_path=_cfg.DB_PATH,
                                knowledge_store=_ks,
                                model=_cfg.OLLAMA_MODEL,
                            )
                except Exception:  # nosec B110
                    pass
                try:
                    from utils.core.timeline import write_timeline_event

                    await asyncio.to_thread(
                        write_timeline_event,
                        "INFO",
                        "ha_repairs",
                        f"HA repair resolved: {translation_key or issue_key}",
                        {
                            "issue_key": issue_key,
                            "translation_key": translation_key,
                            "resolution": "external",
                        },
                    )
                except Exception:  # nosec B110
                    pass

        _repair_outcome = (
            "No issues" if not active_keys else f"{len(active_keys)} issue(s) found"
        )
        try:
            from utils.agent.supervisor import get_supervisor_instance as _get_sv

            _sv_inst = _get_sv()
            if _sv_inst is not None:
                _sv_inst.touch("repair_poll", outcome=_repair_outcome)
        except Exception:  # nosec B110
            pass


async def trigger_remediation_pipeline() -> None:
    """Invokes the Sandbox & Swap Engine with a fresh correlation ID for this repair cycle."""
    cid = str(uuid.uuid4())
    set_correlation_id(cid)
    log.info("repair_cycle_started")
    try:
        from . import ha_agent_sandbox_engine

        await ha_agent_sandbox_engine.main()
    except Exception as e:
        log.error("remediation_failed", error=str(e))


# ==========================================
# MAIN EXECUTION ENTRY
# ==========================================
async def main(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    gate: Optional[AutonomyGate] = None,
    notifier: Optional[NotifierProtocol] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
    ha_ws_client: Optional[HAWebSocketClientProtocol] = None,
) -> None:
    setup_logging()
    _ssh = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    asyncio.create_task(
        ResourcePoller(
            ssh_client=_ssh,
            notifier=_notifier,
            interval_seconds=RESOURCE_POLL_INTERVAL_SECONDS,
            disk_warn_gb=HA_DISK_WARN_GB,
            disk_critical_gb=HA_DISK_CRITICAL_GB,
            mem_warn_mb=HA_MEM_WARN_MB,
        ).run()
    )
    if HA_UPDATE_CHECK_INTERVAL_HOURS > 0 and HA_API_TOKEN:
        asyncio.create_task(
            poll_for_updates(ha_rest_client=ha_rest_client, notifier=_notifier)
        )
    if HA_NOTIFICATION_POLL_INTERVAL_MINUTES > 0 and HA_API_TOKEN:
        asyncio.create_task(
            poll_for_notifications(
                notifier=_notifier,
                llm_client=llm_client,
                ha_ws_client=ha_ws_client,
            )
        )
    if HA_REPAIR_POLL_INTERVAL_MINUTES > 0 and HA_API_TOKEN:
        asyncio.create_task(
            poll_for_repairs(ha_ws_client=ha_ws_client, notifier=_notifier)
        )
    await tail_remote_log_stream(
        ssh_client=_ssh,
        llm_client=llm_client,
        gate=gate,
        notifier=_notifier,
    )


if __name__ == "__main__":
    asyncio.run(main())
