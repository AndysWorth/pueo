"""LLM-guided investigation for HACS integration setup planning."""

from typing import Any, Optional

from utils.core.logging import get_logger

log = get_logger("integration_setup_investigation")


async def run_setup_investigation(
    domain: str,
    notifier: Any,  # NotifierProtocol
    db_path: str,
    llm_client: Optional[Any] = None,  # LLMClientProtocol
    knowledge_store: Optional[Any] = None,
    ha_rest_client: Optional[Any] = None,
    ssh_client: Optional[Any] = None,  # SSHClientProtocol
    ws_client: Optional[Any] = None,  # HAWebSocketClientProtocol
    triggering_card_key: Optional[str] = None,
) -> None:
    """Run an AgentLoop to plan an integration setup and create a HITL card.

    Uses ReadOnlySSHClient (ADR 043) so the investigation cannot write to HA.
    Uses a real AutonomyGate so REST writes (if any future tool is added) are
    properly gated. The plan card (CARD_TYPE_INTEGRATION_SETUP) is sent by
    finish_setup_plan when create_hitl_card=True.
    """
    import asyncio

    import config as _cfg
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.autonomy import AutonomyGate
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_setup_tool_registry
    from utils.core.prompts import load_agent_prompt
    from utils.ha.read_only_ssh import ReadOnlySSHClient
    from utils.ha.ssh_client import AsyncSSHClient
    from utils.knowledge.runbook_signature import sig_setup
    from utils.llm.llm_factory import make_llm_client

    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = AutonomyGate(_cfg.AUTONOMY_LEVEL)
    _ssh_real = ssh_client or AsyncSSHClient(  # pragma: no cover
        _cfg.HA_HOST, _cfg.HA_USER, _cfg.SSH_KEY_PATH
    )

    executor = ToolExecutor(
        ha_ssh_client=ReadOnlySSHClient(_ssh_real),
        gate=gate,
        notifier=notifier,
        ha_rest_client=ha_rest_client,
        ha_ws_client=ws_client,
        knowledge_store=knowledge_store,
        db_path=db_path,
    )

    try:
        from utils.ha.ha_environment import load_environment_profile as _lep

        _prof = await asyncio.to_thread(_lep, db_path)
        if _prof is not None:
            executor.set_ha_profile(_prof)
    except Exception:  # nosec B110
        pass

    registry = build_setup_tool_registry()
    system_prompt = load_agent_prompt("setup", "finish_setup_plan")
    signature = sig_setup(domain)
    subject_key = domain
    knowledge_query = f"integration setup {domain} config flow steps"

    initial_context = (
        f"Integration setup request:\n"
        f"Domain: {domain}\n"
        "The integration is downloaded via HACS but has no active config entry. "
        "Plan each config-flow step with answers and evidence so the user can approve "
        "the plan before Pueo drives the flow."
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
        terminal_tool_name="finish_setup_plan",
        trigger="setup_investigation",
        activity_type="setup_investigation",
        db_path=db_path,
        knowledge_store=knowledge_store,
        timeline_callback=make_activity_timeline_callback(
            "setup_investigation",
            trigger=f"Setup: {domain}",
        ),
        capture_llm=True,
    )
    increment_active_agent()
    try:
        result = await loop.run(
            initial_context=initial_context,
            knowledge_query=knowledge_query,
            signature=signature,
            subject_key=subject_key,
        )
        publish_activity_done("setup_investigation", result.outcome)

        # Resolve the triggering hacs_unloaded card so it doesn't linger.
        if triggering_card_key and db_path:
            import sqlite3 as _sqlite3

            from utils.hitl.hitl_tracker import mark_card_resolved

            try:
                with _sqlite3.connect(db_path) as _conn:
                    mark_card_resolved(_conn, triggering_card_key)
            except Exception:  # nosec B110
                pass

        if result.outcome not in ("success", "awaiting_approval"):
            import sqlite3 as _sqlite3

            from utils.hitl.hitl_tracker import mark_investigation_backoff

            with _sqlite3.connect(db_path) as _conn:
                mark_investigation_backoff(
                    _conn,
                    f"setup_investigation_backoff:{domain}",
                )
            log.warning(
                "setup_investigation_backoff",
                outcome=result.outcome,
                domain=domain,
            )
    except Exception as exc:
        log.error(
            "setup_investigation_failed",
            domain=domain,
            error=str(exc),
        )
    finally:
        decrement_active_agent()
