"""NetAlertX AI diagnosis — converts health anomalies and config issues into a structured
NetAlertXDiagnostic using local Ollama inference (item 17).

Returns None when there is nothing to diagnose (no anomalies, no config issues).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field

import config as _config
from utils.core.context import estimate_tokens
from utils.hitl.llm_trace import LLMTrace
from utils.core.logging import get_logger
from utils.llm.llm_factory import _default_model_for_provider, make_llm_client
from utils.core.prompts import load_agent_prompt

if TYPE_CHECKING:
    from interfaces import (
        HARestClientProtocol,
        HAWebSocketClientProtocol,
        KnowledgeStoreClientProtocol,
        LLMClientProtocol,
        SSHClientProtocol,
    )
    from netalertx.config_validator import ConfigIssue
    from netalertx.health import HealthReport
    from utils.agent.autonomy import AutonomyGate
    from utils.agent.tool_registry import AgentLoopResult
    from utils.ha.ha_environment import HAEnvironmentProfile
    from utils.hitl.notify import NotifierProtocol

log = get_logger("netalertx.diagnosis")


class NetAlertXDiagnostic(BaseModel):
    issue: str = Field(description="Short description of the identified problem.")
    severity: str = Field(description="LOW | MEDIUM | HIGH | CRITICAL")
    category: Literal[
        "networking", "config", "database", "version", "ha_integration", "availability"
    ] = Field(
        description=(
            "networking | config | database | version | ha_integration | availability"
        )
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
    gate: Optional["AutonomyGate"] = None,
    notifier: Optional["NotifierProtocol"] = None,
    rest_client: Optional["HARestClientProtocol"] = None,
    ws_client: Optional["HAWebSocketClientProtocol"] = None,
    ha_profile: Optional["HAEnvironmentProfile"] = None,
    db_path: Optional[str] = None,
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
        from utils.agent.work_queue import (
            PRIORITY_HIGH,
            WorkItem,
            get_work_queue_or_none,
        )

        _wq = get_work_queue_or_none()
        if _wq is not None:
            _result_future: asyncio.Future = asyncio.get_event_loop().create_future()
            _ctx_snap, _c_snap, _m_snap = context, client, model
            _ssh_snap, _ks_snap = ssh_client, knowledge_store
            _gate_snap, _notifier_snap = gate, notifier
            _rest_snap, _ws_snap = rest_client, ws_client
            _prof_snap, _dp_snap = ha_profile, db_path

            async def _diag_coro(
                _ctx: str = _ctx_snap,
                _c: "LLMClientProtocol" = _c_snap,
                _m: str = _m_snap,
                _ssh: "SSHClientProtocol" = _ssh_snap,
                _ks: Optional["KnowledgeStoreClientProtocol"] = _ks_snap,
                _g: Optional["AutonomyGate"] = _gate_snap,
                _n: Optional["NotifierProtocol"] = _notifier_snap,
                _rc: Optional["HARestClientProtocol"] = _rest_snap,
                _wc: Optional["HAWebSocketClientProtocol"] = _ws_snap,
                _hp: Optional["HAEnvironmentProfile"] = _prof_snap,
                _dp: Optional[str] = _dp_snap,
            ) -> None:
                try:
                    _r = await _diagnose_with_agent_loop(
                        _ctx, _c, _m, _ssh, _ks, _g, _n, _rc, _wc, _hp, _dp
                    )
                    if not _result_future.done():
                        _result_future.set_result(_r)
                except Exception as _exc:
                    if not _result_future.done():
                        _result_future.set_exception(_exc)

            await _wq.submit(
                WorkItem(
                    priority=PRIORITY_HIGH,
                    activity_type="netalertx_diagnosis",
                    description="NetAlertX health diagnosis",
                    dedup_key="netalertx_diagnosis",
                    suppress_while_running=frozenset(),
                    coro_factory=_diag_coro,
                )
            )
            return await _result_future
        return await _diagnose_with_agent_loop(
            context,
            client,
            model,
            ssh_client,
            knowledge_store,
            gate,
            notifier,
            rest_client,
            ws_client,
            ha_profile,
            db_path,
        )
    raise RuntimeError(
        "diagnose_health_report: ssh_client is required; the one-shot fallback was removed"
    )


async def _diagnose_with_agent_loop(
    context: str,
    client: "LLMClientProtocol",
    model: str,
    ssh_client: "SSHClientProtocol",
    knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
    gate: Optional["AutonomyGate"] = None,
    notifier: Optional["NotifierProtocol"] = None,
    rest_client: Optional["HARestClientProtocol"] = None,
    ws_client: Optional["HAWebSocketClientProtocol"] = None,
    ha_profile: Optional["HAEnvironmentProfile"] = None,
    db_path: Optional[str] = None,
) -> tuple[Optional[NetAlertXDiagnostic], Optional[LLMTrace]]:
    """Run an AgentLoop to diagnose NetAlertX health anomalies adaptively."""
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.autonomy import AutonomyGate
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_health_diagnosis_registry
    from utils.ha.read_only_ssh import ReadOnlySSHClient
    from utils.hitl.notify import FakeNotifier

    _gate = gate or AutonomyGate(_config.AUTONOMY_LEVEL)
    _notifier = notifier or FakeNotifier()
    _db = db_path or _config.DB_PATH

    initial_message = (
        "Diagnose the following NetAlertX health issues:\n\n"
        + context
        + "\n\nCall query_knowledge for known patterns, use search_log if you need "
        "additional log context, then call finish_health_diagnosis with your diagnosis."
    )

    executor = ToolExecutor(
        ha_ssh_client=ReadOnlySSHClient(ssh_client),
        gate=_gate,
        notifier=_notifier,
        knowledge_store=knowledge_store,
        ha_ws_client=ws_client,
        ha_rest_client=rest_client,
        db_path=_db,
    )
    if ha_profile is not None:
        executor.set_ha_profile(ha_profile)
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

    from utils.agent.supervisor import (
        decrement_active_agent,
        increment_active_agent,
        make_activity_timeline_callback,
        publish_activity_done,
    )

    loop = AgentLoop(
        llm_client=client,
        tool_executor=executor,
        tool_registry=registry,
        model=model,
        terminal_tool_name="finish_health_diagnosis",
        trigger="health_diagnosis",
        activity_type="netalertx_investigation",
        knowledge_store=knowledge_store,
        db_path=_db,
        on_llm_call_start=_on_diag_start,
        on_llm_call_done=_on_diag_done,
        capture_llm=True,
        timeline_callback=make_activity_timeline_callback(
            "netalertx_investigation", trigger="NetAlertX health check"
        ),
    )

    from utils.knowledge.runbook_signature import sig_netalertx

    increment_active_agent()
    try:
        loop_result = await loop.run(
            initial_message, signature=sig_netalertx("availability")
        )
        publish_activity_done("netalertx_investigation", loop_result.outcome)
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
            system_prompt=load_agent_prompt("config_repair", "finish_health_diagnosis"),
            user_prompt=initial_message,
            raw_response=result.model_dump_json() if result else "",
        )
        return result, trace
    except Exception as exc:
        log.error("netalertx_diagnosis_agent_loop_failed", error=str(exc))
        return None, None
    finally:
        decrement_active_agent()
