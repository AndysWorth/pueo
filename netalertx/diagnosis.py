"""NetAlertX AI diagnosis — converts health anomalies and config issues into a structured
NetAlertXDiagnostic using local Ollama inference (item 17).

Returns None when there is nothing to diagnose (no anomalies, no config issues).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

from config import OLLAMA_MODEL
from utils.context import estimate_tokens
from utils.logging import get_logger
from utils.ollama_client import OllamaClient
from utils.prompts import load_prompt

if TYPE_CHECKING:
    from interfaces import LLMClientProtocol
    from netalertx.config_validator import ConfigIssue
    from netalertx.health import HealthReport

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


async def diagnose_health_report(
    report: "HealthReport",
    config_issues: Optional[list["ConfigIssue"]] = None,
    llm_client: Optional["LLMClientProtocol"] = None,
) -> Optional[NetAlertXDiagnostic]:
    """Return a NetAlertXDiagnostic, or None if there is nothing to diagnose."""
    all_issues = list(config_issues or [])

    if not report.anomalies and not all_issues:
        return None

    client = llm_client or OllamaClient()
    system_prompt = load_prompt("diagnose_netalertx")
    context = _build_context(report, all_issues)
    user_prompt = f"Diagnose the following NetAlertX issues:\n\n{context}"

    _ = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)

    try:
        response = await client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},
            format=NetAlertXDiagnostic.model_json_schema(),
        )
        result = NetAlertXDiagnostic.model_validate_json(response["message"]["content"])
        log.info(
            "netalertx_diagnosis_complete",
            category=result.category,
            severity=result.severity,
        )
        return result
    except Exception as exc:
        log.error("netalertx_diagnosis_inference_failed", error=str(exc))
        return None
