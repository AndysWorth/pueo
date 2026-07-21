"""Evidence-first installer diagnostics — item 22.

Gathers SSH observations when an installer step fails, calls local Ollama to produce
a structured InstallerDiagnostic, and formats it for HITL notification bodies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

from config import OLLAMA_MODEL
from utils.context import estimate_tokens, truncate_to_budget
from utils.logging import get_logger
from utils.ollama_client import OllamaClient
from utils.prompts import load_prompt

if TYPE_CHECKING:
    from interfaces import LLMClientProtocol, SSHClientProtocol

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
    _, out, _ = await ssh_client.run("ha addons info core_mosquitto")
    evidence["addon_info"] = out
    _, out, _ = await ssh_client.run("ha addons logs core_mosquitto -n 50")
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
    _, out, _ = await ssh_client.run(f"ha addons info {slug}")
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
    _, out, _ = await ssh_client.run(f"ha addons info {slug}")
    evidence["addon_info"] = out
    _, out, _ = await ssh_client.run(f"ha addons logs {slug} -n 50")
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


async def diagnose_installer_failure(
    failure_type: str,
    ssh_client: "SSHClientProtocol",
    llm_client: Optional["LLMClientProtocol"] = None,
    slug: str = "",
) -> InstallerDiagnostic:
    """Gather evidence and return a structured LLM diagnosis for an installer failure.

    failure_type: "mosquitto_start" | "addon_install" | "addon_start"
    slug: required for "addon_install" and "addon_start" failure types.
    """
    if failure_type == "mosquitto_start":
        evidence = await gather_mosquitto_evidence(ssh_client)
    elif failure_type == "addon_install":
        evidence = await gather_addon_install_evidence(ssh_client, slug)
    else:
        evidence = await gather_addon_start_evidence(ssh_client, slug)

    context = _build_evidence_context(failure_type, evidence)
    system_prompt = load_prompt("diagnose_installer")
    user_prompt = f"Diagnose the following installer failure:\n\n{context}"

    log.info(
        "installer_diagnosis_start",
        failure_type=failure_type,
        slug=slug,
        evidence_tokens=estimate_tokens(context),
    )

    client = llm_client or OllamaClient()
    try:
        response = await client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},
            format=InstallerDiagnostic.model_json_schema(),
        )
        result = InstallerDiagnostic.model_validate_json(response["message"]["content"])
        log.info(
            "installer_diagnosis_complete",
            failure_type=failure_type,
            confidence=result.confidence,
            can_auto_fix=result.can_auto_fix,
        )
        return result
    except Exception as exc:
        log.error("installer_diagnosis_failed", error=str(exc))
        return InstallerDiagnostic(
            primary_hypothesis="Diagnosis unavailable — LLM inference failed.",
            confidence=0.0,
            supporting_evidence=[],
            alternative_hypotheses=[],
            recommended_action="Review the logs manually and re-run setup.",
            can_auto_fix=False,
        )


def format_diagnostic_for_hitl(diagnostic: InstallerDiagnostic) -> str:
    """Render InstallerDiagnostic as human-readable text for HITL notification body."""
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
