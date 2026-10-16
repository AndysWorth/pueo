"""LLM-guided investigation for host-health findings (swap, eMMC wear, reboot, resolution)."""

import asyncio
from typing import Any, Optional

from utils.core.logging import get_logger
from utils.disk.host_health import HostHealthFinding

log = get_logger("host_health_investigation")


def host_health_knowledge_query(finding: HostHealthFinding) -> str:
    """Targeted pre-inject query: kind plus resolution issue type or reason."""
    detail = finding.evidence.get("type") or finding.evidence.get("reason") or ""
    return f"host health {finding.kind} {detail}".strip()


async def run_host_health_investigation(
    finding: HostHealthFinding,
    notifier: Any,  # NotifierProtocol
    db_path: str,
    llm_client: Optional[Any] = None,  # LLMClientProtocol
    knowledge_store: Optional[Any] = None,
    ha_rest_client: Optional[Any] = None,
    ssh_client: Optional[Any] = None,  # SSHClientProtocol
) -> None:
    """Run an AgentLoop to investigate a host-health finding and create a HITL card.

    Uses the real AutonomyGate(AUTONOMY_LEVEL) and a real AsyncSSHClient so
    gated fixes (ha resolution suggestion apply, ha apps restart) actually
    execute or become cards depending on the autonomy level — unlike repair-issue
    investigations, which use a FakeAutonomyGate.
    """
    import config as _cfg
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.autonomy import AutonomyGate
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_host_health_tool_registry
    from utils.core.prompts import load_prompt
    from utils.ha.ssh_client import AsyncSSHClient
    from utils.llm.llm_factory import make_llm_client

    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = AutonomyGate(_cfg.AUTONOMY_LEVEL)
    _ssh = ssh_client or AsyncSSHClient(  # pragma: no cover
        _cfg.HA_HOST, _cfg.HA_USER, _cfg.SSH_KEY_PATH
    )

    executor = ToolExecutor(
        ha_ssh_client=_ssh,
        gate=gate,
        notifier=notifier,
        ha_rest_client=ha_rest_client,
        knowledge_store=knowledge_store,
        db_path=db_path,
    )
    executor.set_host_health_finding(finding)

    try:
        from utils.ha.ha_environment import load_environment_profile as _lep

        _prof = await asyncio.to_thread(_lep, db_path)
        if _prof is not None:
            executor.set_ha_profile(_prof)
    except Exception:  # nosec B110
        pass

    registry = build_host_health_tool_registry()
    system_prompt = load_prompt("agent_loop_host_health").format(
        terminal_tool="finish_host_health"
    )

    evidence_lines = "\n".join(
        f"  {k}: {v}" for k, v in finding.evidence.items() if v is not None
    )
    initial_context = (
        f"Host health issue detected:\n"
        f"Kind: {finding.kind}\n"
        f"Severity: {finding.severity}\n"
        f"Evidence:\n{evidence_lines}"
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
        terminal_tool_name="finish_host_health",
        trigger="resource_poll",
        activity_type="host_health",
        db_path=db_path,
        knowledge_store=knowledge_store,
        timeline_callback=make_activity_timeline_callback(
            "host_health", trigger=f"Host health: {finding.kind}"
        ),
        capture_llm=True,
    )
    from utils.knowledge.runbook_signature import sig_host_health

    increment_active_agent()
    try:
        result = await loop.run(
            initial_context=initial_context,
            knowledge_query=host_health_knowledge_query(finding),
            signature=sig_host_health(finding.kind, finding.evidence.get("type", "")),
            subject_key=finding.key,
        )
        publish_activity_done("host_health", result.outcome)
        if result.outcome != "success":
            import sqlite3 as _sqlite3

            from utils.hitl.hitl_tracker import mark_investigation_backoff

            with _sqlite3.connect(db_path) as _conn:
                mark_investigation_backoff(_conn, f"host_health_backoff:{finding.key}")
            log.warning(
                "host_health_investigation_backoff",
                outcome=result.outcome,
                finding_key=finding.key,
            )
    except Exception as exc:
        log.error(
            "host_health_investigation_failed",
            finding_key=finding.key,
            error=str(exc),
        )
    finally:
        decrement_active_agent()
