"""NetAlertX log monitor — SSH tail of app.log with two-layer AI triage (item 15)."""

from __future__ import annotations

import asyncio
import collections
import re
from typing import Optional

from pydantic import BaseModel, Field

from config import (
    AUTONOMY_LEVEL,
    CONFIDENCE_THRESHOLD,
    DEBOUNCE_WINDOW_SECONDS,
    HITL_TIMEOUT_MINUTES,
    MAX_PROMPT_TOKENS,
    MAX_REPAIRS_PER_HOUR,
    NETALERTX_LOG_CONTAINER_NAME,
    NETALERTX_SSH_HOST,
    NETALERTX_SSH_KEY_PATH,
    NETALERTX_SSH_USER,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    OLLAMA_MODEL,
    SSH_RETRY_BASE_DELAY,
)
from interfaces import LLMClientProtocol, SSHClientProtocol
from utils.autonomy import AutonomyGate, RiskLevel
from utils.context import estimate_tokens, sliding_window_lines
from utils.logging import get_logger, setup_logging
from utils.notify import NotifierProtocol, get_notifier
from utils.ollama_client import OllamaClient
from utils.prompts import load_prompt
from utils.rate_limiter import Debouncer, RateLimitExceeded, RateLimiter
from utils.retry import async_retry
from utils.ssh_client import AsyncSSHClient

log = get_logger("netalertx.log_monitor")

_debouncer = Debouncer(DEBOUNCE_WINDOW_SECONDS)
_rate_limiter = RateLimiter(MAX_REPAIRS_PER_HOUR, 3600)
_log_buffer: collections.deque[str] = collections.deque(maxlen=50)

CRITICAL_LOG_PATTERN = re.compile(
    r"(ERROR|CRITICAL|Exception|Traceback).*(scan|MQTT|plugin|broker|arp|database)",
    re.IGNORECASE,
)


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
) -> LogEvaluation:
    """Uses local Ollama to classify whether recent NetAlertX log context contains an actionable error."""
    client = llm_client or OllamaClient()
    system_prompt = load_prompt("triage_netalertx_log")
    user_envelope = "Evaluate these NetAlertX log lines:\n```\n\n```"
    overhead = estimate_tokens(system_prompt) + estimate_tokens(user_envelope)
    windowed = sliding_window_lines(recent_lines, MAX_PROMPT_TOKENS - overhead)
    log_context = "\n".join(windowed)
    user_prompt = f"Evaluate these NetAlertX log lines:\n```\n{log_context}\n```"

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
        return LogEvaluation.model_validate_json(response["message"]["content"])
    except Exception as e:
        log.error("netalertx_triage_inference_failed", error=str(e))
        return LogEvaluation(
            is_actionable=False,
            root_cause_summary="Inference crash",
            confidence_score=0.0,
        )


async def _dispatch_to_healer(evaluation: LogEvaluation) -> None:
    """Placeholder for healer dispatch — wired in item 18."""
    log.info(
        "netalertx_healer_dispatch_pending",
        cause=evaluation.root_cause_summary,
    )


@async_retry(max_attempts=0, base_delay=SSH_RETRY_BASE_DELAY, exceptions=(OSError,))
async def tail_netalertx_log_stream(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    gate: Optional[AutonomyGate] = None,
    notifier: Optional[NotifierProtocol] = None,
) -> None:
    """Streams NetAlertX app.log via SSH and applies two-layer triage on matching lines."""
    client = ssh_client or AsyncSSHClient(
        NETALERTX_SSH_HOST, NETALERTX_SSH_USER, NETALERTX_SSH_KEY_PATH
    )
    _gate = gate or AutonomyGate(AUTONOMY_LEVEL, HITL_TIMEOUT_MINUTES)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

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

            if CRITICAL_LOG_PATTERN.search(clean_line):
                log.warning("netalertx_log_intercepted", line=clean_line)
                evaluation = await analyze_log_line_with_ai(
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
                            },
                        )
                        continue

                    log.warning("netalertx_repair_triggered")
                    await _dispatch_to_healer(evaluation)

    except Exception as e:
        log.error("netalertx_log_stream_failed", error=str(e))
        log.info("netalertx_log_stream_reconnect")
        raise


async def main(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    gate: Optional[AutonomyGate] = None,
    notifier: Optional[NotifierProtocol] = None,
) -> None:
    setup_logging()
    await tail_netalertx_log_stream(
        ssh_client=ssh_client,
        llm_client=llm_client,
        gate=gate,
        notifier=notifier,
    )


if __name__ == "__main__":
    asyncio.run(main())
