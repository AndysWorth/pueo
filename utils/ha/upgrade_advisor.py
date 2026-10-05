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

read_post_upgrade_report() reads the post_upgrade_status and
post_upgrade_regressions attributes after a completed update.  It returns
None when the integration is absent or the attributes are empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from interfaces import HARestClientProtocol

_MAX_REPORT_CHARS = 2000
_MAX_REGRESSIONS = 20


@dataclass
class AdvisorReport:
    status: str  # "report_ready" (always when returned)
    risk: str  # "low" | "medium" | "high" | "unknown"
    breaking_change_count: int
    report: str  # truncated to _MAX_REPORT_CHARS
    available_version: str


@dataclass
class PostUpgradeReport:
    post_upgrade_status: str  # e.g. "ok", "regressions", or arbitrary string
    regressions: List[str] = field(default_factory=list)


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


async def read_post_upgrade_report(
    rest: "HARestClientProtocol",
) -> Optional[PostUpgradeReport]:
    """Return post-upgrade status and regressions from the advisor sensor.

    Reads sensor.upgrade_advisor_status attributes post_upgrade_status and
    post_upgrade_regressions.  Returns None when the integration is absent or
    neither attribute is populated (advisor has not analysed this update yet).
    """
    try:
        status_entity = await rest.get_state("sensor.upgrade_advisor_status")
    except Exception:  # nosec B110 — not installed; skip silently
        return None

    attrs = status_entity.get("attributes") or {}
    post_status = str(attrs.get("post_upgrade_status") or "").strip()
    raw_regressions = attrs.get("post_upgrade_regressions") or []

    if not post_status and not raw_regressions:
        return None

    if isinstance(raw_regressions, str):
        regressions = [raw_regressions] if raw_regressions.strip() else []
    else:
        regressions = [str(r) for r in list(raw_regressions)][:_MAX_REGRESSIONS]

    return PostUpgradeReport(
        post_upgrade_status=post_status,
        regressions=regressions,
    )
