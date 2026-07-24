#!/usr/bin/env python3
"""Layer 4 — continuous log streaming via 'ha core logs --follow' with two-layer AI triage and repair triggering."""

import asyncio
import collections
import re
import uuid
from typing import Optional
from pydantic import BaseModel, Field

from config import (
    HA_HOST,
    HA_USER,
    SSH_KEY_PATH,
    OLLAMA_MODEL,
    CONFIDENCE_THRESHOLD,
    SELF_HEALING_ENABLED,
    SSH_RETRY_BASE_DELAY,
    DEBOUNCE_WINDOW_SECONDS,
    REPAIR_COOLDOWN_SECONDS,
    MAX_REPAIRS_PER_HOUR,
    MAX_PROMPT_TOKENS,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    AUTONOMY_LEVEL,
    RESOURCE_POLL_INTERVAL_SECONDS,
    HA_DISK_WARN_GB,
    HA_DISK_CRITICAL_GB,
    HA_MEM_WARN_MB,
)
from interfaces import LLMClientProtocol, SSHClientProtocol
from utils.context import estimate_tokens, sliding_window_lines
from utils.llm_trace import LLMTrace
from utils.logging import get_logger, setup_logging, set_correlation_id
from utils.ollama_client import OllamaClient
from utils.prompts import load_prompt
from utils.autonomy import AutonomyGate, RiskLevel
from utils.notify import NotifierProtocol, get_notifier
from utils.rate_limiter import Debouncer, RateLimiter, RateLimitExceeded
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
    await tail_remote_log_stream(
        ssh_client=_ssh,
        llm_client=llm_client,
        gate=gate,
        notifier=_notifier,
    )


if __name__ == "__main__":
    asyncio.run(main())
