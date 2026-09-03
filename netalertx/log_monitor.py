"""NetAlertX log monitor — SSH tail of app.log with two-layer AI triage (item 15)."""

from __future__ import annotations

import asyncio
import collections
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

import config as _config
from config import (
    AUTONOMY_LEVEL,
    CONFIDENCE_THRESHOLD,
    DEBOUNCE_WINDOW_SECONDS,
    MAX_PROMPT_TOKENS,
    MAX_REPAIRS_PER_HOUR,
    NETALERTX_LOG_CONTAINER_NAME,
    NETALERTX_SEVERITY_CONFIDENCE_THRESHOLD,
    NETALERTX_SSH_HOST,
    NETALERTX_SSH_KEY_PATH,
    NETALERTX_SSH_USER,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    SSH_RETRY_BASE_DELAY,
)
from interfaces import LLMClientProtocol, SSHClientProtocol
from utils.agent.autonomy import AutonomyGate, RiskLevel
from utils.core.context import estimate_tokens, sliding_window_lines
from utils.hitl.llm_trace import LLMTrace
from utils.core.logging import get_logger, setup_logging
from utils.hitl.notify import NotifierProtocol, get_notifier
from utils.llm.llm_factory import _default_model_for_provider, make_llm_client
from utils.core.prompts import load_prompt
from utils.core.rate_limiter import Debouncer, RateLimitExceeded, RateLimiter
from utils.core.retry import async_retry
from utils.ha.ssh_client import AsyncSSHClient

if TYPE_CHECKING:
    from netalertx.diagnosis import NetAlertXDiagnostic
    from netalertx.healer import NetAlertXHealer

log = get_logger("netalertx.log_monitor")

_debouncer = Debouncer(DEBOUNCE_WINDOW_SECONDS)
_rate_limiter = RateLimiter(MAX_REPAIRS_PER_HOUR, 3600)
_log_buffer: collections.deque[str] = collections.deque(maxlen=50)

# Sparkline state — 60-minute ring buffer; each bucket is (bucket_ts, total_lines, match_lines).
_LOOP_NAME = "netalertx"
_RETENTION_SECONDS = 31 * 24 * 3600  # 31 days

_sparkline_buckets: collections.deque = collections.deque(maxlen=60)
_sparkline_current_minute: int = 0
_sparkline_bucket_total: int = 0
_sparkline_bucket_matches: int = 0
_sparkline_bucket_min_ts: int = (
    0  # epoch of first log line in current bucket (0 = none yet)
)
_last_line_at: float = 0.0  # epoch of most recent received line
_last_match_at: float = 0.0  # epoch of most recent CRITICAL_LOG_PATTERN match
_last_scan_at: float = 0.0  # epoch of most recent scan-detected log line

CRITICAL_LOG_PATTERN = re.compile(
    r"(ERROR|CRITICAL|Exception|Traceback).*(scan|MQTT|plugin|broker|arp|database)",
    re.IGNORECASE,
)

# Matches log lines indicating a NetAlertX scan cycle completed or found devices.
_SCAN_LOG_PATTERN = re.compile(
    r"(scan.*(start|complet|done|finish|found)|arp.*(start|complet|done)|"
    r"devices.*found|new.*device|scanning)",
    re.IGNORECASE,
)


def _init_sparkline_db() -> None:
    """Pre-populate ring buffer from SQLite and prune old rows."""
    global _sparkline_current_minute
    try:
        with sqlite3.connect(_config.DB_PATH) as conn:
            conn.execute(
                "DELETE FROM stream_metrics WHERE loop_name = ? AND bucket_ts < ?",
                (_LOOP_NAME, int(time.time()) - _RETENTION_SECONDS),
            )
            rows = conn.execute(
                "SELECT bucket_ts, total_lines, match_lines FROM stream_metrics "
                "WHERE loop_name = ? ORDER BY bucket_ts DESC LIMIT 60",
                (_LOOP_NAME,),
            ).fetchall()
        for row in reversed(rows):
            _sparkline_buckets.append((row[0], row[1], row[2]))
        _sparkline_current_minute = int(time.time() // 60)
    except Exception:  # nosec B110
        pass


def _persist_sparkline_bucket(
    bucket_ts: int, total: int, matches: int, min_line_ts: int
) -> None:
    try:
        with sqlite3.connect(_config.DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO stream_metrics "
                "(loop_name, bucket_ts, total_lines, match_lines, min_line_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (_LOOP_NAME, bucket_ts, total, matches, min_line_ts or None),
            )
    except Exception:  # nosec B110
        pass


def _record_sparkline_line(matched: bool, scan: bool) -> None:
    """Update sparkline ring buffer for one received NetAlertX log line."""
    global _sparkline_current_minute, _sparkline_bucket_total, _sparkline_bucket_matches
    global _sparkline_bucket_min_ts, _last_line_at, _last_match_at, _last_scan_at

    now = time.time()
    _last_line_at = now
    if matched:
        _last_match_at = now
    if scan:
        _last_scan_at = now

    current_min = int(now // 60)
    if current_min != _sparkline_current_minute:
        completed_ts = _sparkline_current_minute * 60
        _sparkline_buckets.append(
            (completed_ts, _sparkline_bucket_total, _sparkline_bucket_matches)
        )
        _persist_sparkline_bucket(
            completed_ts,
            _sparkline_bucket_total,
            _sparkline_bucket_matches,
            _sparkline_bucket_min_ts,
        )
        _sparkline_bucket_total = 0
        _sparkline_bucket_matches = 0
        _sparkline_bucket_min_ts = 0
        _sparkline_current_minute = current_min

    if _sparkline_bucket_total == 0:
        _sparkline_bucket_min_ts = int(now)
    _sparkline_bucket_total += 1
    if matched:
        _sparkline_bucket_matches += 1


def get_netalertx_sparkline_data(
    bucket_size: str = "1m", time_range: str = "1h"
) -> dict:
    """Return sparkline data for the /loops/netalertx/sparkline endpoint."""
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
                "WHERE loop_name = ? AND bucket_ts >= ? ORDER BY bucket_ts",
                (_LOOP_NAME, cutoff_ts),
            ).fetchall()
    except Exception:  # nosec B110
        rows = []

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

    if _sparkline_current_minute:
        cur_key = (_sparkline_current_minute * 60 // bucket_seconds) * bucket_seconds
        if cur_key not in agg:
            agg[cur_key] = [0, 0, None]
        agg[cur_key][0] += _sparkline_bucket_total
        agg[cur_key][1] += _sparkline_bucket_matches
        if _sparkline_bucket_min_ts:
            if agg[cur_key][2] is None or _sparkline_bucket_min_ts < agg[cur_key][2]:
                agg[cur_key][2] = _sparkline_bucket_min_ts

    # Fill empty bucket slots so every time position is represented (cap at 1440)
    first_key = (cutoff_ts // bucket_seconds) * bucket_seconds
    end_key = (now_ts // bucket_seconds) * bucket_seconds
    slot = first_key
    slot_count = 0
    while slot <= end_key and slot_count < 1440:
        if slot not in agg:
            agg[slot] = [0, 0, None]
        slot += bucket_seconds
        slot_count += 1

    buckets = [
        [v[2] if v[2] is not None else k, v[0], v[1]] for k, v in sorted(agg.items())
    ]
    return {
        "buckets": buckets,
        "bucket_size": bucket_size,
        "time_range": time_range,
        "last_line_at": _last_line_at,
        "last_match_at": _last_match_at,
        "last_scan_at": _last_scan_at,
        "configured": bool(_config.NETALERTX_HOST),
    }


class LogEvaluation(BaseModel):
    is_actionable: bool = Field(
        description="True if this log indicates a NetAlertX error that a corrective action can fix."
    )
    root_cause_summary: str = Field(
        description="Brief string summarizing what failed (e.g., 'ArpScan failed: network unreachable')."
    )
    confidence_score: float = Field(
        description="Value between 0.0 and 1.0 evaluating certainty."
    )


async def analyze_log_line_with_ai(
    recent_lines: list[str],
    llm_client: Optional[LLMClientProtocol] = None,
) -> tuple[LogEvaluation, LLMTrace]:
    """Uses local Ollama to classify whether recent NetAlertX log context contains an actionable error."""
    client = llm_client or make_llm_client()
    system_prompt = load_prompt("triage_netalertx_log")
    user_envelope = "Evaluate these NetAlertX log lines:\n```\n\n```"
    overhead = estimate_tokens(system_prompt) + estimate_tokens(user_envelope)
    windowed = sliding_window_lines(recent_lines, MAX_PROMPT_TOKENS - overhead)
    log_context = "\n".join(windowed)
    user_prompt = f"Evaluate these NetAlertX log lines:\n```\n{log_context}\n```"

    model = _default_model_for_provider()
    _triage_armed = False
    try:
        from utils.agent.supervisor import (
            decrement_active_triage,
            increment_active_triage,
            publish_event,
        )

        increment_active_triage()
        publish_event({"type": "triage_start"})
        _triage_armed = True
    except Exception:  # nosec B110
        pass
    try:
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},
            format=LogEvaluation.model_json_schema(),
        )
        raw_output = response["message"]["content"]
        trace = LLMTrace(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_response=raw_output,
        )
        return LogEvaluation.model_validate_json(raw_output), trace
    except Exception as e:
        log.error("netalertx_triage_inference_failed", error=str(e))
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
                publish_event({"type": "triage_done"})
            except Exception:  # nosec B110
                pass


def _evaluation_to_diagnostic(evaluation: LogEvaluation) -> "NetAlertXDiagnostic":
    """Convert a LogEvaluation to a minimal NetAlertXDiagnostic for healer dispatch."""
    from netalertx.diagnosis import NetAlertXDiagnostic

    summary = evaluation.root_cause_summary.lower()
    if any(kw in summary for kw in ("mqtt", "broker", "mosquitto")):
        category = "mqtt"
    elif any(kw in summary for kw in ("scan", "arp", "network", "unreachable")):
        category = "networking"
    elif any(kw in summary for kw in ("database", "db")):
        category = "database"
    else:
        category = "networking"

    severity = (
        "HIGH"
        if evaluation.confidence_score > NETALERTX_SEVERITY_CONFIDENCE_THRESHOLD
        else "MEDIUM"
    )

    return NetAlertXDiagnostic(
        issue=evaluation.root_cause_summary,
        severity=severity,
        category=category,
        recommended_fix="See log context for details.",
        affected_netalertx_version="unknown",
    )


async def _dispatch_to_healer(
    evaluation: LogEvaluation,
    healer: Optional["NetAlertXHealer"] = None,
) -> None:
    if healer is None:
        log.info(
            "netalertx_healer_dispatch_skipped",
            cause=evaluation.root_cause_summary,
        )
        return
    diagnostic = _evaluation_to_diagnostic(evaluation)
    log.info(
        "netalertx_healer_dispatch",
        cause=evaluation.root_cause_summary,
        category=diagnostic.category,
    )
    await healer.heal(diagnostic)


@async_retry(max_attempts=0, base_delay=SSH_RETRY_BASE_DELAY, exceptions=(OSError,))
async def tail_netalertx_log_stream(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    gate: Optional[AutonomyGate] = None,
    notifier: Optional[NotifierProtocol] = None,
    healer: Optional["NetAlertXHealer"] = None,
) -> None:
    """Streams NetAlertX app.log via SSH and applies two-layer triage on matching lines."""
    client = ssh_client or AsyncSSHClient(
        NETALERTX_SSH_HOST, NETALERTX_SSH_USER, NETALERTX_SSH_KEY_PATH
    )
    _gate = gate or AutonomyGate(AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    _init_sparkline_db()

    log_path = "/data/app.log"
    tail_command = f"docker exec {NETALERTX_LOG_CONTAINER_NAME} tail -F {log_path}"
    log.info(
        "netalertx_log_stream_start",
        host=NETALERTX_SSH_HOST,
        log_path=log_path,
    )

    try:
        async for line in client.stream_lines(tail_command):
            clean_line = line.strip()
            _log_buffer.append(clean_line)

            _matched = bool(CRITICAL_LOG_PATTERN.search(clean_line))
            _scan = bool(_SCAN_LOG_PATTERN.search(clean_line))
            _record_sparkline_line(_matched, _scan)

            if _matched:
                log.warning("netalertx_log_intercepted", line=clean_line)
                evaluation, llm_trace = await analyze_log_line_with_ai(
                    list(_log_buffer), llm_client=llm_client
                )
                log.info(
                    "netalertx_triage_complete",
                    actionable=evaluation.is_actionable,
                    cause=evaluation.root_cause_summary,
                    confidence=evaluation.confidence_score,
                )

                if (
                    evaluation.is_actionable
                    and evaluation.confidence_score > CONFIDENCE_THRESHOLD
                ):
                    if not _debouncer.record():
                        log.info("debounce_suppressed")
                        continue
                    try:
                        _rate_limiter.check()
                    except RateLimitExceeded:
                        log.warning("rate_limit_exceeded")
                        continue

                    if not _gate.should_auto_execute(RiskLevel.HIGH):
                        log.info(
                            "autonomy_gate_blocked",
                            cause=evaluation.root_cause_summary,
                        )
                        await _notifier.send(
                            subject="Pueo: NetAlertX log event — approval required",
                            body=evaluation.root_cause_summary,
                            payload={
                                "cause": evaluation.root_cause_summary,
                                "confidence": evaluation.confidence_score,
                                "diagnosis": evaluation.model_dump(),
                                "evidence_raw": {
                                    "log_buffer_snapshot": list(_log_buffer)
                                },
                                "llm_trace": llm_trace.as_dict(),
                            },
                        )
                        continue

                    log.warning("netalertx_repair_triggered")
                    asyncio.create_task(_dispatch_to_healer(evaluation, healer=healer))

    except Exception as e:
        log.error("netalertx_log_stream_failed", error=str(e))
        log.info("netalertx_log_stream_reconnect")
        raise


async def main(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    gate: Optional[AutonomyGate] = None,
    notifier: Optional[NotifierProtocol] = None,
    healer: Optional["NetAlertXHealer"] = None,
) -> None:
    setup_logging()
    await tail_netalertx_log_stream(
        ssh_client=ssh_client,
        llm_client=llm_client,
        gate=gate,
        notifier=notifier,
        healer=healer,
    )


if __name__ == "__main__":
    asyncio.run(main())
