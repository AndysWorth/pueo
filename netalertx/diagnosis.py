"""NetAlertX AI diagnosis — converts health anomalies and config issues into a structured
NetAlertXDiagnostic using local Ollama inference (item 17).

Returns None when there is nothing to diagnose (no anomalies, no config issues).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

import config as _config
from utils.core.context import estimate_tokens
from utils.hitl.llm_trace import LLMTrace
from utils.core.logging import get_logger
from utils.llm.llm_factory import _default_model_for_provider, make_llm_client
from utils.core.prompts import load_prompt

if TYPE_CHECKING:
    from interfaces import (
        KnowledgeStoreClientProtocol,
        LLMClientProtocol,
        SSHClientProtocol,
    )
    from netalertx.config_validator import ConfigIssue
    from netalertx.health import HealthReport
    from utils.agent.tool_registry import AgentLoopResult

log = get_logger("netalertx.diagnosis")


class NetAlertXDiagnostic(BaseModel):
    issue: str = Field(description="Short description of the identified problem.")
    severity: str = Field(description="LOW | MEDIUM | HIGH | CRITICAL")
    category: str = Field(
        description="networking | mqtt | database | version | ha_integration"
    )
    recommended_fix: str = Field(
        description="Concrete remediation steps, including relevant commands or config changes."
    )
    affected_netalertx_version: str = Field(
        description="NetAlertX version from the health report, or 'unknown'."
    )


def _build_context(
    report: "HealthReport",
    config_issues: list["ConfigIssue"],
) -> str:
    parts: list[str] = []

    if report.anomalies:
        parts.append(
            "Health anomalies:\n" + "\n".join(f"- {a}" for a in report.anomalies)
        )
    if config_issues:
        parts.append(
            "Configuration issues:\n"
            + "\n".join(
                f"- [{i.severity}] {i.field}: {i.message}" for i in config_issues
            )
        )

    parts.append(f"Device counts: {report.device_counts}")
    parts.append(f"Scan age (minutes): {report.last_scan_age_minutes}")
    parts.append(f"MQTT active: {report.mqtt_active}")
    parts.append(f"NetAlertX version: {report.netalertx_version}")

    return "\n\n".join(parts)


def _extract_health_diagnostic(
    loop_result: "AgentLoopResult",
) -> Optional[NetAlertXDiagnostic]:
    """Extract NetAlertXDiagnostic from the terminal tool step in an AgentLoopResult."""
    for step in loop_result.steps:
        if step.tool_call.name == "finish_health_diagnosis":
            args = step.tool_call.arguments
            try:
                return NetAlertXDiagnostic(
                    issue=args.get("issue", ""),
                    severity=args.get("severity", "MEDIUM"),
                    category=args.get("category", "networking"),
                    recommended_fix=args.get("recommended_fix", ""),
                    affected_netalertx_version=args.get(
                        "affected_netalertx_version", "unknown"
                    ),
                )
            except Exception:
                return None
    return None


async def diagnose_health_report(
    report: "HealthReport",
    config_issues: Optional[list["ConfigIssue"]] = None,
    llm_client: Optional["LLMClientProtocol"] = None,
    ssh_client: Optional["SSHClientProtocol"] = None,
    knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
) -> tuple[Optional[NetAlertXDiagnostic], Optional[LLMTrace]]:
    """Return a (NetAlertXDiagnostic, LLMTrace) tuple, or (None, None) if nothing to diagnose.

    When ssh_client is provided, runs an AgentLoop that can gather additional log
    evidence adaptively. Without ssh_client, falls back to a direct one-shot LLM call.
    """
    all_issues = list(config_issues or [])

    if not report.anomalies and not all_issues:
        return None, None

    client = llm_client or make_llm_client()
    model = _default_model_for_provider()
    context = _build_context(report, all_issues)

    if ssh_client is not None:
        return await _diagnose_with_agent_loop(
            context, client, model, ssh_client, knowledge_store
        )
    return await _diagnose_one_shot(context, client, model)


async def _diagnose_with_agent_loop(
    context: str,
    client: "LLMClientProtocol",
    model: str,
    ssh_client: "SSHClientProtocol",
    knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
) -> tuple[Optional[NetAlertXDiagnostic], Optional[LLMTrace]]:
    """Run an AgentLoop to diagnose NetAlertX health anomalies adaptively."""
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_health_diagnosis_registry
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.hitl.notify import FakeNotifier

    initial_message = (
        "Diagnose the following NetAlertX health issues:\n\n"
        + context
        + "\n\nCall query_knowledge for known patterns, use search_log if you need "
        "additional log context, then call finish_health_diagnosis with your diagnosis."
    )

    executor = ToolExecutor(
        ha_ssh_client=ssh_client,
        gate=FakeAutonomyGate(),  # type: ignore[arg-type]
        notifier=FakeNotifier(),
    )
    registry = build_health_diagnosis_registry()
    try:
        from utils.agent.supervisor import (
            clear_llm_active as _clr_llm,
            set_llm_active as _set_llm,
        )

        def _on_diag_start(m: str, t: str) -> None:
            _set_llm("netalertx_diagnosis", m)

        def _on_diag_done(m: str, lat: float) -> None:
            _clr_llm("netalertx_diagnosis")

    except Exception:  # nosec B110
        _on_diag_start = None  # type: ignore[assignment]
        _on_diag_done = None  # type: ignore[assignment]

    loop = AgentLoop(
        llm_client=client,
        tool_executor=executor,
        tool_registry=registry,
        model=model,
        terminal_tool_name="finish_health_diagnosis",
        trigger="health_diagnosis",
        knowledge_store=knowledge_store,
        on_llm_call_start=_on_diag_start,
        on_llm_call_done=_on_diag_done,
    )

    try:
        loop_result = await loop.run(initial_message)
        result = _extract_health_diagnostic(loop_result)
        if result is not None:
            log.info(
                "netalertx_diagnosis_complete",
                category=result.category,
                severity=result.severity,
                outcome=loop_result.outcome,
            )
        trace = LLMTrace(
            model=model,
            system_prompt=load_prompt("agent_loop").format(
                terminal_tool="finish_health_diagnosis"
            ),
            user_prompt=initial_message,
            raw_response=result.model_dump_json() if result else "",
        )
        return result, trace
    except Exception as exc:
        log.error("netalertx_diagnosis_agent_loop_failed", error=str(exc))
        return None, None


async def _diagnose_one_shot(
    context: str,
    client: "LLMClientProtocol",
    model: str,
) -> tuple[Optional[NetAlertXDiagnostic], Optional[LLMTrace]]:
    """One-shot LLM call for NetAlertX health diagnosis (fallback without ssh_client)."""
    system_prompt = load_prompt("diagnose_netalertx")
    user_prompt = f"Diagnose the following NetAlertX issues:\n\n{context}"
    try:
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},
            format=NetAlertXDiagnostic.model_json_schema(),
        )
        raw_output = response["message"]["content"]
        result = NetAlertXDiagnostic.model_validate_json(raw_output)
        log.info(
            "netalertx_diagnosis_complete",
            category=result.category,
            severity=result.severity,
        )
        trace = LLMTrace(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_response=raw_output,
        )
        return result, trace
    except Exception as exc:
        log.error("netalertx_diagnosis_inference_failed", error=str(exc))
        return None, None
