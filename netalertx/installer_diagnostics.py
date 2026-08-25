"""Evidence-first installer diagnostics — item 22.

Gathers SSH observations when an installer step fails, calls local Ollama to produce
a structured InstallerDiagnostic, and formats it for approval notification bodies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

import config as _config
from utils.core.context import estimate_tokens, truncate_to_budget
from utils.hitl.llm_trace import LLMTrace
from utils.core.logging import get_logger
from utils.llm.llm_factory import _default_model_for_provider, make_llm_client

if TYPE_CHECKING:
    from interfaces import LLMClientProtocol, SSHClientProtocol
    from utils.agent.tool_registry import AgentLoopResult

log = get_logger("netalertx.installer_diagnostics")

_EVIDENCE_TOKEN_BUDGET = 2000


class InstallerDiagnostic(BaseModel):
    primary_hypothesis: str = Field(
        description="Most likely cause in plain English, e.g. 'Port 1883 is in use by another process'"
    )
    confidence: float = Field(
        description="Certainty 0.0–1.0. Below 0.6 means evidence is insufficient to conclude."
    )
    supporting_evidence: list[str] = Field(
        description="Specific observations from the evidence that support the primary hypothesis. "
        "Cite exact log lines or command output — do not infer."
    )
    alternative_hypotheses: list[str] = Field(
        description="Other possible causes not ruled out by the evidence."
    )
    recommended_action: str = Field(
        description="Concrete, specific action to resolve the issue. Include exact commands or UI steps."
    )
    can_auto_fix: bool = Field(
        description="True only if the fix can be executed via a single SSH command with no side effects."
    )
    auto_fix_command: Optional[str] = Field(
        default=None,
        description="The exact SSH command to run if can_auto_fix is True.",
    )
    verification_command: Optional[str] = Field(
        default=None,
        description="SSH command to run after the fix to confirm it worked.",
    )


async def gather_mosquitto_evidence(
    ssh_client: "SSHClientProtocol",
) -> dict[str, str]:
    """Gather diagnostic evidence when core_mosquitto fails to start."""
    evidence: dict[str, str] = {}
    _, out, _ = await ssh_client.run("ha apps info core_mosquitto")
    evidence["addon_info"] = out
    _, out, _ = await ssh_client.run("ha apps logs core_mosquitto -n 50")
    evidence["addon_logs"] = out
    _, out, _ = await ssh_client.run("ss -tlnp | grep 1883")
    evidence["port_1883"] = out or "(nothing listening on 1883)"
    _, out, _ = await ssh_client.run("ha supervisor info")
    evidence["supervisor_info"] = out
    return evidence


async def gather_addon_install_evidence(
    ssh_client: "SSHClientProtocol",
    slug: str,
) -> dict[str, str]:
    """Gather diagnostic evidence when a named add-on fails to install."""
    evidence: dict[str, str] = {}
    _, out, _ = await ssh_client.run(f"ha apps info {slug}")
    evidence["addon_info"] = out
    _, out, _ = await ssh_client.run("ha supervisor info")
    evidence["supervisor_info"] = out
    return evidence


async def gather_addon_start_evidence(
    ssh_client: "SSHClientProtocol",
    slug: str,
) -> dict[str, str]:
    """Gather diagnostic evidence when a named add-on fails to reach running state."""
    evidence: dict[str, str] = {}
    _, out, _ = await ssh_client.run(f"ha apps info {slug}")
    evidence["addon_info"] = out
    _, out, _ = await ssh_client.run(f"ha apps logs {slug} -n 50")
    evidence["addon_logs"] = out
    return evidence


def _build_evidence_context(failure_type: str, evidence: dict[str, str]) -> str:
    """Format evidence dict into a token-budgeted prompt context string."""
    per_key = _EVIDENCE_TOKEN_BUDGET // max(len(evidence), 1)
    parts = [f"Failure type: {failure_type}"]
    for key, value in evidence.items():
        truncated = truncate_to_budget(value.strip(), per_key, strategy="tail")
        parts.append(f"[{key}]\n{truncated}")
    return "\n\n".join(parts)


def _fallback_diagnostic() -> InstallerDiagnostic:
    return InstallerDiagnostic(
        primary_hypothesis="Diagnosis unavailable — agent loop did not complete.",
        confidence=0.0,
        supporting_evidence=[],
        alternative_hypotheses=[],
        recommended_action="Review the logs manually and re-run setup.",
        can_auto_fix=False,
    )


def _extract_installer_diagnostic(
    loop_result: "AgentLoopResult",
) -> InstallerDiagnostic:
    """Extract InstallerDiagnostic from the terminal tool step in an AgentLoopResult."""
    for step in loop_result.steps:
        if step.tool_call.name == "finish_installer_diagnosis":
            args = step.tool_call.arguments
            try:
                return InstallerDiagnostic(
                    primary_hypothesis=args.get("primary_hypothesis", ""),
                    confidence=float(args.get("confidence", 0.0)),
                    supporting_evidence=args.get("supporting_evidence", []),
                    alternative_hypotheses=args.get("alternative_hypotheses", []),
                    recommended_action=args.get("recommended_action", ""),
                    can_auto_fix=bool(args.get("can_auto_fix", False)),
                    auto_fix_command=args.get("auto_fix_command"),
                    verification_command=args.get("verification_command"),
                )
            except Exception:
                return _fallback_diagnostic()
    return _fallback_diagnostic()


async def diagnose_installer_failure(
    failure_type: str,
    ssh_client: "SSHClientProtocol",
    llm_client: Optional["LLMClientProtocol"] = None,
    slug: str = "",
) -> tuple[InstallerDiagnostic, LLMTrace, dict[str, str]]:
    """Gather evidence then run an AgentLoop to produce a structured diagnosis.

    failure_type: "mosquitto_start" | "addon_install" | "addon_start"
    slug: required for "addon_install" and "addon_start" failure types.
    Returns a 3-tuple: (diagnostic, llm_trace, evidence_dict).
    """
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import (
        AgentLoopResult,
        build_installer_diagnosis_registry,
    )
    from utils.agent.autonomy import FakeAutonomyGate
    from utils.hitl.notify import FakeNotifier

    if failure_type == "mosquitto_start":
        evidence = await gather_mosquitto_evidence(ssh_client)
    elif failure_type == "addon_install":
        evidence = await gather_addon_install_evidence(ssh_client, slug)
    else:
        evidence = await gather_addon_start_evidence(ssh_client, slug)

    context = _build_evidence_context(failure_type, evidence)

    log.info(
        "installer_diagnosis_start",
        failure_type=failure_type,
        slug=slug,
        evidence_tokens=estimate_tokens(context),
    )

    client = llm_client or make_llm_client()
    model = _default_model_for_provider()
    initial_message = (
        f"Diagnose this installer failure: {failure_type}"
        + (f" (add-on: {slug})" if slug else "")
        + ".\n\n"
        "Here is the gathered evidence:\n\n"
        + context
        + "\n\nCall query_knowledge for known patterns, then call "
        "finish_installer_diagnosis with your structured diagnosis."
    )

    executor = ToolExecutor(
        ha_ssh_client=ssh_client,
        gate=FakeAutonomyGate(),  # type: ignore[arg-type]
        notifier=FakeNotifier(),
    )
    registry = build_installer_diagnosis_registry()
    loop = AgentLoop(
        llm_client=client,
        tool_executor=executor,
        tool_registry=registry,
        model=model,
        terminal_tool_name="finish_installer_diagnosis",
        trigger="installer_diagnosis",
    )

    try:
        loop_result: AgentLoopResult = await loop.run(initial_message)
        diagnostic = _extract_installer_diagnostic(loop_result)
        log.info(
            "installer_diagnosis_complete",
            failure_type=failure_type,
            confidence=diagnostic.confidence,
            can_auto_fix=diagnostic.can_auto_fix,
            outcome=loop_result.outcome,
        )
    except Exception as exc:
        log.error("installer_diagnosis_failed", error=str(exc))
        diagnostic = _fallback_diagnostic()

    from utils.core.prompts import load_prompt as _lp

    trace = LLMTrace(
        model=model,
        system_prompt=_lp("agent_loop").format(
            terminal_tool="finish_installer_diagnosis"
        ),
        user_prompt=initial_message,
        raw_response=diagnostic.model_dump_json(),
    )
    return diagnostic, trace, evidence


def format_diagnostic_for_hitl(diagnostic: InstallerDiagnostic) -> str:
    """Render InstallerDiagnostic as human-readable text for approval notification body."""
    pct = int(diagnostic.confidence * 100)
    lines = [
        f"Diagnosis: {diagnostic.primary_hypothesis} (confidence: {pct}%)",
        "",
    ]

    if diagnostic.supporting_evidence:
        lines.append("Evidence:")
        for item in diagnostic.supporting_evidence:
            lines.append(f"  • {item}")
        lines.append("")

    if diagnostic.alternative_hypotheses:
        others = "; ".join(diagnostic.alternative_hypotheses)
        lines.append(f"Other possibilities: {others}")
        lines.append("")

    lines.append(f"Recommended action: {diagnostic.recommended_action}")

    if diagnostic.can_auto_fix and diagnostic.auto_fix_command:
        lines.append(f"  SSH command: {diagnostic.auto_fix_command}")
        if diagnostic.verification_command:
            lines.append(f"  Verify with: {diagnostic.verification_command}")
        lines.append("")
        lines.append("Pueo can attempt this fix automatically if approved.")

    return "\n".join(lines)
