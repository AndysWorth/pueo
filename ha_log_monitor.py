#!/usr/bin/env python3
"""Layer 4 — continuous log streaming via 'ha core logs --follow' with two-layer AI triage and repair triggering."""

import asyncio
import collections
import datetime
import re
import sqlite3
import uuid
from typing import Optional
from pydantic import BaseModel, Field

from config import (
    DB_PATH,
    HA_API_PORT,
    HA_API_TOKEN,
    HA_DISK_CRITICAL_GB,
    HA_DISK_WARN_GB,
    HA_HOST,
    HA_MEM_WARN_MB,
    HA_NOTIFICATION_POLL_INTERVAL_MINUTES,
    HA_UPDATE_CHECK_INTERVAL_HOURS,
    HA_UPDATE_NOTIFY_ON_AVAILABLE,
    HA_UPDATE_RELEASE_NOTES_CACHE_DIR,
    HA_USER,
    MAX_PROMPT_TOKENS,
    MAX_REPAIRS_PER_HOUR,
    NETALERTX_API_PORT,
    NETALERTX_API_TOKEN,
    NETALERTX_HOST,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    OLLAMA_MODEL,
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
    HARestClientProtocol,
    HAWebSocketClientProtocol,
    LLMClientProtocol,
    NetAlertXClientProtocol,
    SSHClientProtocol,
)
from utils.context import estimate_tokens, sliding_window_lines
from utils.llm_trace import LLMTrace
from utils.logging import get_logger, setup_logging, set_correlation_id
from utils.ollama_client import OllamaClient
from utils.prompts import load_prompt
from utils.autonomy import AutonomyGate, RiskLevel
from utils.notify import NotifierProtocol, get_notifier
from utils.rate_limiter import Debouncer, RateLimiter, RateLimitExceeded
from utils.ha_rest_client import HARestClient, get_update_status
from utils.resource import ResourcePoller
from utils.retry import async_retry
from utils.ssh_client import AsyncSSHClient

log = get_logger("ha_log_monitor")

_debouncer = Debouncer(DEBOUNCE_WINDOW_SECONDS)
_rate_limiter = RateLimiter(MAX_REPAIRS_PER_HOUR, 3600)
_log_buffer: collections.deque[str] = collections.deque(maxlen=50)

# High-priority regex to capture structural components collapsing or syntax crashes
CRITICAL_LOG_PATTERN = re.compile(
    r"(ERROR|CRITICAL).*?(Component error|Failed to initialize|Traceback|Invalid config|Error doing job)",
    re.IGNORECASE,
)


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
    client = llm_client or OllamaClient()

    system_prompt = load_prompt("triage_log")
    user_envelope = "Evaluate these log lines:\n```\n\n```"
    overhead = estimate_tokens(system_prompt) + estimate_tokens(user_envelope)
    windowed = sliding_window_lines(recent_lines, MAX_PROMPT_TOKENS - overhead)
    log_context = "\n".join(windowed)
    user_prompt = f"Evaluate these log lines:\n```\n{log_context}\n```"

    try:
        response = await client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},
            format=LogEvaluation.model_json_schema(),
        )
        raw_output = response["message"]["content"]
        trace = LLMTrace(
            model=OLLAMA_MODEL,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_response=raw_output,
        )
        return LogEvaluation.model_validate_json(raw_output), trace
    except Exception as e:
        log.error("triage_inference_failed", error=str(e))
        return LogEvaluation(
            is_actionable=False,
            root_cause_summary="Inference crash",
            confidence_score=0.0,
        ), LLMTrace(
            model=OLLAMA_MODEL,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_response="",
        )


# ==========================================
# STREAMING SSH CONNECTION LAYER
# ==========================================
@async_retry(max_attempts=0, base_delay=SSH_RETRY_BASE_DELAY, exceptions=(OSError,))
async def tail_remote_log_stream(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    gate: Optional[AutonomyGate] = None,
    notifier: Optional[NotifierProtocol] = None,
) -> None:
    """Streams live HA logs via 'ha core logs --follow' over SSH."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    _gate = gate or AutonomyGate(AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    log.info("log_stream_start", host=HA_HOST)

    tail_command = "ha core logs --follow"

    try:
        async for line in client.stream_lines(tail_command):
            clean_line = line.strip()
            _log_buffer.append(clean_line)

            if CRITICAL_LOG_PATTERN.search(clean_line):
                log.warning("log_line_intercepted", line=clean_line)
                log.info("triage_start", model=OLLAMA_MODEL)
                evaluation, llm_trace = await analyze_log_line_with_ai(
                    list(_log_buffer), llm_client=llm_client
                )
                log.info(
                    "triage_complete",
                    actionable=evaluation.is_actionable,
                    cause=evaluation.root_cause_summary,
                    confidence=evaluation.confidence_score,
                )

                if (
                    evaluation.is_actionable
                    and evaluation.confidence_score > CONFIDENCE_THRESHOLD
                ):
                    try:  # pragma: no cover
                        from utils.timeline import write_timeline_event

                        write_timeline_event(
                            "ERROR",
                            "ha_log_monitor",
                            evaluation.root_cause_summary,
                            {
                                "confidence": evaluation.confidence_score,
                                "log_line": clean_line,
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
                        continue
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
                            subject="Pueo: Actionable log event — approval required",
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
                    log.warning("repair_triggered")
                    asyncio.create_task(trigger_remediation_pipeline())
                    log.info(
                        "repair_cooldown_start",
                        seconds=REPAIR_COOLDOWN_SECONDS,
                    )
                    await asyncio.sleep(REPAIR_COOLDOWN_SECONDS)
                    log.info("repair_cooldown_complete")

    except Exception as e:
        log.error("log_stream_failed", error=str(e))
        log.info("log_stream_reconnect")
        raise


async def poll_for_updates(
    ha_rest_client: Optional[HARestClientProtocol] = None,
    notifier: Optional[NotifierProtocol] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    cache_dir: Optional[str] = None,
) -> None:
    """Periodically checks for available HA updates and fires full HITL approval cards."""
    from ha_update_manager import (
        UpdateReadinessReport,
        analyze_breaking_changes,
        fetch_release_notes_cached,
    )
    from utils.card_types import CARD_TYPE_UPDATE

    interval = HA_UPDATE_CHECK_INTERVAL_HOURS * 3600
    _client: HARestClientProtocol = ha_rest_client or HARestClient(
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    _ssh: Optional[SSHClientProtocol] = ssh_client  # None → no config fetch (soft)
    _llm: Optional[LLMClientProtocol] = (
        llm_client  # None → OllamaClient() inside analyze
    )
    _cache_dir = cache_dir or HA_UPDATE_RELEASE_NOTES_CACHE_DIR
    # Track which entity_ids have already triggered a notification so we don't repeat
    _notified: set[str] = set()

    while True:
        try:
            updates = await get_update_status(_client)
        except Exception as exc:
            log.warning("update_poll_failed", error=str(exc))
            await asyncio.sleep(interval)
            continue

        for u in updates:
            if u.update_available and u.entity_id not in _notified:
                log.info(
                    "update_available",
                    component=u.component,
                    installed=u.installed_version,
                    latest=u.latest_version,
                )

                # Run breaking-change analysis for core updates.
                readiness: Optional[UpdateReadinessReport] = None
                if u.component == "core":
                    config_yaml_content = ""
                    if _ssh is not None:
                        try:
                            from config import CONFIG_REMOTE_PATH

                            config_yaml_content = await _ssh.read_file(
                                CONFIG_REMOTE_PATH
                            )
                        except Exception as exc:
                            log.warning(
                                "update_poll_config_fetch_failed", error=str(exc)
                            )

                    release_notes = ""
                    try:
                        release_notes = await fetch_release_notes_cached(
                            u.latest_version, _cache_dir
                        )
                    except Exception as exc:
                        log.warning(
                            "update_poll_release_notes_failed",
                            version=u.latest_version,
                            error=str(exc),
                        )

                    if release_notes:
                        try:
                            readiness = await analyze_breaking_changes(
                                u, config_yaml_content, release_notes, _llm
                            )
                        except Exception as exc:
                            log.warning("update_poll_analysis_failed", error=str(exc))

                if HA_UPDATE_NOTIFY_ON_AVAILABLE:
                    risk = (
                        "CRITICAL"
                        if u.component in ("core", "os")
                        else "HIGH" if u.component == "supervisor" else "MEDIUM"
                    )
                    body_parts = [
                        f"Component: {u.component}",
                        f"Risk: {risk}",
                    ]
                    if u.release_summary:
                        body_parts.append(f"Summary: {u.release_summary}")
                    if readiness:
                        advisory = (
                            "SAFE" if readiness.safe_to_update else "REVIEW REQUIRED"
                        )
                        body_parts.append(
                            f"Advisory: {advisory} — {readiness.recommendation}"
                        )

                    await _notifier.send(
                        subject=(
                            f"Update available: {u.component}"
                            f" {u.installed_version} → {u.latest_version}"
                        ),
                        body="\n".join(body_parts),
                        payload={
                            "card_type": CARD_TYPE_UPDATE,
                            "component": u.component,
                            "entity_id": u.entity_id,
                            "installed_version": u.installed_version,
                            "latest_version": u.latest_version,
                            "release_url": u.release_url,
                            "release_summary": u.release_summary,
                            "risk": risk,
                            "severity": risk,
                            "breaking_changes": (
                                readiness.breaking_changes if readiness else []
                            ),
                            "affected_config_keys": (
                                readiness.affected_config_keys if readiness else []
                            ),
                            "pueo_command_risks": (
                                readiness.pueo_command_risks if readiness else []
                            ),
                            "advisory": readiness.recommendation if readiness else None,
                            "safe_to_update": (
                                readiness.safe_to_update if readiness else None
                            ),
                        },
                    )
                try:  # pragma: no cover
                    from utils.timeline import write_timeline_event

                    write_timeline_event(
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
                _notified.add(u.entity_id)
            elif not u.update_available:
                # Clear the flag once the update entity goes back to "off"
                _notified.discard(u.entity_id)

        await asyncio.sleep(interval)


async def poll_for_notifications(
    notifier: Optional[NotifierProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    ha_ws_client: Optional[HAWebSocketClientProtocol] = None,
    netalertx_client: Optional[NetAlertXClientProtocol] = None,
    db_path: str = DB_PATH,
) -> None:
    """Periodically checks for new HA persistent notifications and fires HITL alerts."""
    from ha_notification_manager import (
        _format_notification_body,
        _format_notification_subject,
        classify_notification,
        enrich_and_analyze_notification,
        mark_notification_hitl_sent,
        record_notification_seen,
    )
    from netalertx.api_client import NetAlertXAPIClient
    from utils.ha_ws_client import HAWebSocketClient

    interval = HA_NOTIFICATION_POLL_INTERVAL_MINUTES * 60
    _ws: HAWebSocketClientProtocol = ha_ws_client or HAWebSocketClient(
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )  # pragma: no cover
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    _llm: LLMClientProtocol = llm_client or OllamaClient()  # pragma: no cover
    _ssh: SSHClientProtocol = ssh_client or AsyncSSHClient(
        HA_HOST, HA_USER, SSH_KEY_PATH
    )  # pragma: no cover
    _nax: NetAlertXClientProtocol = (
        netalertx_client
        or NetAlertXAPIClient(  # pragma: no cover
            f"http://{NETALERTX_HOST}:{NETALERTX_API_PORT}", NETALERTX_API_TOKEN
        )
    )

    while True:
        try:
            notifications = await _ws.get_persistent_notifications()
        except Exception as exc:
            log.warning("notification_poll_failed", error=str(exc))
            await asyncio.sleep(interval)
            continue

        for notif in notifications:
            nid: str = notif.get("notification_id", "")
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
                    pass

            category, severity = classify_notification(nid)
            record_notification_seen(
                nid, category, severity, db_path=db_path, ha_created_at=ha_created_at
            )

            with sqlite3.connect(db_path) as _conn:
                _row = _conn.execute(
                    "SELECT hitl_sent_at FROM notification_history WHERE notification_id = ?",
                    (nid,),
                ).fetchone()
            should_send = _row is not None and _row[0] is None

            if should_send:
                try:
                    analysis = await enrich_and_analyze_notification(
                        nid,
                        title,
                        message,
                        ssh_client=_ssh,
                        llm_client=_llm,
                        netalertx_client=_nax,
                        ws_client=_ws,
                    )
                except Exception as exc:
                    log.warning(
                        "notification_enrichment_failed",
                        notification_id=nid,
                        error=str(exc),
                    )
                    continue

                log.info(
                    "new_notification_detected",
                    notification_id=nid,
                    category=analysis.category,
                    severity=analysis.severity,
                )
                card_id = f"notif_{nid}"
                payload: dict = {
                    "notification_id": card_id,
                    "ha_notification_id": nid,
                    "is_notification_card": True,
                    "category": analysis.category,
                    "severity": analysis.severity,
                    "human_explanation": analysis.human_explanation,
                    "recommended_action": analysis.recommended_action,
                    "enriched_context": analysis.enriched_context,
                    "original_message": analysis.original_message,
                    "original_title": analysis.original_title,
                    "ha_created_at": ha_created_at,
                }
                await _notifier.send(
                    subject=_format_notification_subject(analysis),
                    body=_format_notification_body(analysis),
                    payload=payload,
                )
                mark_notification_hitl_sent(nid, db_path=db_path)

        await asyncio.sleep(interval)


async def trigger_remediation_pipeline() -> None:
    """Invokes the Sandbox & Swap Engine with a fresh correlation ID for this repair cycle."""
    cid = str(uuid.uuid4())
    set_correlation_id(cid)
    log.info("repair_cycle_started")
    try:
        import ha_agent_sandbox_engine

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
                ssh_client=_ssh,
                ha_ws_client=ha_ws_client,
            )
        )
    await tail_remote_log_stream(
        ssh_client=_ssh,
        llm_client=llm_client,
        gate=gate,
        notifier=_notifier,
    )


if __name__ == "__main__":
    asyncio.run(main())
