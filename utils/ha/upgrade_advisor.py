"""Read the ha-upgrade-advisor sensor state for update-analysis context.

The ha-upgrade-advisor integration (HACS: brianegge/ha-upgrade-advisor) exposes:
  sensor.upgrade_advisor_status  — state: idle / analyzing / report_ready / error
                                   attrs: current_version, available_version,
                                          breaking_change_count, report,
                                          post_upgrade_status, post_upgrade_regressions
  sensor.upgrade_advisor_risk    — state: low / medium / high

read_advisor_report() returns a populated AdvisorReport only when:
  - state == "report_ready"
  - available_version matches the target_version being analysed

Otherwise it returns None so the caller falls back to its own LLM analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from interfaces import HARestClientProtocol

_MAX_REPORT_CHARS = 2000


@dataclass
class AdvisorReport:
    status: str  # "report_ready" (always when returned)
    risk: str  # "low" | "medium" | "high" | "unknown"
    breaking_change_count: int
    report: str  # truncated to _MAX_REPORT_CHARS
    available_version: str


async def read_advisor_report(
    rest: "HARestClientProtocol",
    target_version: str,
) -> Optional[AdvisorReport]:
    """Return an AdvisorReport when a ready, version-matching report exists.

    Returns None when the integration is absent, the sensor is not in
    "report_ready" state, or the report covers a different target version.
    Callers should treat None as "advisor not available" and proceed without it.
    """
    try:
        status_entity = await rest.get_state("sensor.upgrade_advisor_status")
    except Exception:  # nosec B110 — not installed; skip silently
        return None

    state = status_entity.get("state", "")
    if state != "report_ready":
        return None

    attrs = status_entity.get("attributes") or {}
    available_version = attrs.get("available_version", "")
    if available_version != target_version:
        # Stale report for a different version — don't use it
        return None

    report = str(attrs.get("report") or "")
    if len(report) > _MAX_REPORT_CHARS:
        report = report[:_MAX_REPORT_CHARS] + "...[truncated]"

    raw_count = attrs.get("breaking_change_count", 0)
    try:
        breaking_change_count = int(raw_count)
    except (TypeError, ValueError):
        breaking_change_count = 0

    risk = "unknown"
    try:
        risk_entity = await rest.get_state("sensor.upgrade_advisor_risk")
        risk = risk_entity.get("state") or "unknown"
    except Exception:  # nosec B110 — risk sensor absent is fine
        pass

    return AdvisorReport(
        status=state,
        risk=risk,
        breaking_change_count=breaking_change_count,
        report=report,
        available_version=available_version,
    )
