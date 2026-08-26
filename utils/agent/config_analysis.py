"""Shared HA configuration analysis — single authoritative implementation.

Consolidates three previously duplicate analyze_config_locally() functions from
ha_agent_core.py, ha_agent_advanced.py, and ha_agent_sandbox_engine.py.

Hybrid strategy:
  - With ssh_client: runs an AgentLoop that can follow !include directives,
    call ha core check, and query the knowledge base before reporting.
  - Without ssh_client: falls back to a one-shot LLM call (backward-compatible
    for tests and callers that do not have an SSH connection available).

DiagnosticsReport and LLMTrace are imported lazily inside functions to avoid
circular imports with agents.ha_agent_core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from utils.core.prompts import load_prompt
from utils.core.context import estimate_tokens, truncate_to_budget
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from agents.ha_agent_core import DiagnosticsReport
    from utils.hitl.llm_trace import LLMTrace
    from interfaces import (
        KnowledgeStoreClientProtocol,
        LLMClientProtocol,
        SSHClientProtocol,
    )
    from utils.agent.tool_registry import AgentLoopResult

import config as _config

log = get_logger("config_analysis")

MAX_PROMPT_TOKENS: int = getattr(_config, "MAX_PROMPT_TOKENS", 8000)


async def analyze_config_locally(
    yaml_content: str,
    ssh_client: Optional["SSHClientProtocol"] = None,
    llm_client: Optional["LLMClientProtocol"] = None,
    knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
) -> tuple["DiagnosticsReport", Optional["LLMTrace"]]:
    """Analyse HA config.yaml for issues.

    Returns ``(DiagnosticsReport, LLMTrace | None)``.  When *ssh_client* is
    provided the analysis runs inside an AgentLoop so the model can follow
    ``!include`` directives and run ``ha core check``.  Without it, a single
    one-shot LLM call is used (preserving the previous behaviour for tests and
    scripts that run without SSH).
    """
    if ssh_client is not None:
        return await _analyze_with_agent_loop(
            yaml_content, ssh_client, llm_client, knowledge_store
        )
    return await _analyze_one_shot(yaml_content, llm_client)


async def _analyze_with_agent_loop(
    yaml_content: str,
    ssh_client: "SSHClientProtocol",
    llm_client: Optional["LLMClientProtocol"],
    knowledge_store: Optional["KnowledgeStoreClientProtocol"] = None,
) -> tuple["DiagnosticsReport", Optional["LLMTrace"]]:
    from utils.llm.llm_factory import make_llm_client
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import (
        build_config_analysis_registry,
        AgentLoopResult,
    )
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.hitl.notify import FakeNotifier

    client = llm_client or make_llm_client()
    snippet = truncate_to_budget(yaml_content, MAX_PROMPT_TOKENS - 400)
    initial_message = (
        "Analyse the following Home Assistant configuration.yaml for syntax errors, "
        "deprecated formats, and logical risks.\n\n"
        f"```yaml\n{snippet}\n```\n\n"
        "Call query_knowledge for known patterns, use read_file to follow any "
        "!include directives, run ha core check if needed, then call "
        "finish_diagnosis with your structured assessment."
    )

    executor = ToolExecutor(
        ha_ssh_client=ssh_client,
        gate=FakeAutonomyGate(),  # type: ignore[arg-type]
        notifier=FakeNotifier(),
        llm_client=client,
    )
    registry = build_config_analysis_registry()
    loop = AgentLoop(
        llm_client=client,
        tool_executor=executor,
        tool_registry=registry,
        terminal_tool_name="finish_diagnosis",
        trigger="config_analysis",
        knowledge_store=knowledge_store,
    )

    try:
        loop_result: AgentLoopResult = await loop.run(initial_message)
        report = _extract_diagnostics_report(loop_result)
        return report, None
    except Exception as exc:
        log.warning("config_analysis_agent_loop_failed", error=str(exc))
        return await _analyze_one_shot(yaml_content, llm_client)


def _extract_diagnostics_report(loop_result: "AgentLoopResult") -> "DiagnosticsReport":
    from agents.ha_agent_core import DiagnosticsReport

    for step in loop_result.steps:
        if step.tool_call.name == "finish_diagnosis":
            args = step.tool_call.arguments
            try:
                return DiagnosticsReport(
                    is_valid=bool(args.get("is_valid", True)),
                    severity=str(args.get("severity", "NONE")),
                    identified_issues=list(args.get("identified_issues", [])),
                    recommended_fix_yaml=args.get("recommended_fix_yaml"),
                )
            except Exception:  # nosec B110
                pass
    return DiagnosticsReport(
        is_valid=True,
        severity="NONE",
        identified_issues=[],
        recommended_fix_yaml=None,
    )


async def _analyze_one_shot(
    yaml_content: str,
    llm_client: Optional["LLMClientProtocol"],
) -> tuple["DiagnosticsReport", "LLMTrace"]:
    from utils.llm.llm_factory import make_llm_client
    from agents.ha_agent_core import DiagnosticsReport
    from utils.hitl.llm_trace import LLMTrace

    client = llm_client or make_llm_client()
    system_prompt = load_prompt("diagnose_config")
    user_prefix = "Analyze this configuration data:\n\n```yaml\n"
    user_suffix = "\n```"
    overhead = estimate_tokens(system_prompt) + estimate_tokens(
        user_prefix + user_suffix
    )
    content_budget = MAX_PROMPT_TOKENS - overhead
    original_tokens = estimate_tokens(yaml_content)
    if original_tokens > content_budget:
        yaml_content = truncate_to_budget(yaml_content, content_budget, "smart")
        log.warning(
            "content_truncated",
            original_tokens=original_tokens,
            truncated_tokens=estimate_tokens(yaml_content),
        )
    user_prompt = f"{user_prefix}{yaml_content}{user_suffix}"

    try:
        log.info("ollama_analyze_start", model=_config.OLLAMA_MODEL)
        response = await client.chat(
            model=_config.OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},
            format=DiagnosticsReport.model_json_schema(),
        )
        raw_output = response["message"]["content"]
        trace = LLMTrace(
            model=_config.OLLAMA_MODEL,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_response=raw_output,
        )
        return DiagnosticsReport.model_validate_json(raw_output), trace
    except Exception as e:
        log.error("ollama_inference_failed", error=str(e))
        raise
