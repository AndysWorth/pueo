"""Shared cloud-escalation coroutine (ADR 007).

Used by ha_agent_sandbox_engine (autonomy-level-4 auto-path) and
web/dashboard._execute_cloud_escalation (approval path at levels 2–3).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol
    from utils.agent.tool_registry import AgentLoopResult, ToolRegistry

log = get_logger("cloud_escalation")


async def run_cloud_escalation(
    initial_context: str,
    tool_registry: "ToolRegistry",
    gate: Any,
    ha_ssh_client: Any,
    notifier: Any,
    incident_id: str,
    timeline_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
    knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
) -> "AgentLoopResult":
    """Run AgentLoop with ClaudeAPIClient. Raises BillingCapError if caps exceeded."""
    from utils.agent.agent_loop import AgentLoop
    from utils.repair.billing import check_billing_caps
    from utils.llm.cloud_client import ClaudeAPIClient
    from utils.agent.tool_executor import ToolExecutor
    from config import (
        AGENT_MAX_TOOL_CALLS,
        AGENT_MAX_WALL_SECONDS,
        CLOUD_MAX_COST_PER_INCIDENT_USD,
        CLOUD_MAX_DAILY_SPEND_USD,
        DB_PATH,
    )

    check_billing_caps(incident_id, "", 2000, 512)  # raises BillingCapError if over cap

    llm = ClaudeAPIClient(incident_id=incident_id)
    executor = ToolExecutor(
        ha_ssh_client=ha_ssh_client,
        gate=gate,
        notifier=notifier,
    )
    loop = AgentLoop(
        llm_client=llm,
        tool_executor=executor,
        tool_registry=tool_registry,
        max_tool_calls=AGENT_MAX_TOOL_CALLS,
        max_wall_seconds=AGENT_MAX_WALL_SECONDS,
        trigger="escalated",
        db_path=DB_PATH,
        escalated=True,
        timeline_callback=timeline_callback,
        knowledge_store=knowledge_store,
    )

    if not initial_context:
        initial_context = (
            "Analyze the Home Assistant configuration.yaml for issues "
            "and apply a fix if needed."
        )

    try:
        from utils.agent.supervisor import set_active_repair_loop

        set_active_repair_loop(loop)
    except Exception:  # nosec B110 — best-effort registration
        pass
    try:
        result = await loop.run(initial_context)
    finally:
        try:
            from utils.agent.supervisor import set_active_repair_loop

            set_active_repair_loop(None)
        except Exception:  # nosec B110
            pass
    log.info(
        "cloud_escalation_complete",
        outcome=result.outcome,
        steps=len(result.steps),
        incident_id=incident_id,
    )
    return result
