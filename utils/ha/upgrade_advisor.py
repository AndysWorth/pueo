"""Read and trigger the ha-upgrade-advisor integration (HACS: brianegge/ha-upgrade-advisor).

The integration exposes:
  sensor.upgrade_advisor_status     — state: idle / analyzing / report_ready / error
                                      attrs: current_version, available_version,
                                             breaking_change_count, report,
                                             post_upgrade_status, post_upgrade_regressions,
                                             last_analysis, last_post_upgrade_check
  sensor.upgrade_advisor_risk_level — state: low / medium / high

Entity IDs are resolved through the entity registry (platform == "upgrade_advisor") so
they work regardless of the user's customised entity names.  The hard-coded defaults are
used as a fallback when a WS client is not available.

read_advisor_report()       returns a populated AdvisorReport only when:
  - state == "report_ready"
  - For Core updates: available_version matches the target_version being analysed
  - For HACS components: last_analysis is later than trigger_time (when provided)

request_advisor_analysis()  triggers an analysis for a given component version and polls
                            until the sensor leaves "analyzing", then returns.  Callers
                            should call read_advisor_report() afterwards.

read_post_upgrade_report()  reads the post_upgrade_status and post_upgrade_regressions
                            attributes after a completed update.  Returns None when the
                            integration is absent, the attributes are empty, or the report
                            predates update_completed_at (when provided).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional, Tuple

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import HARestClientProtocol, HAWebSocketClientProtocol

log = get_logger("upgrade_advisor")

_MAX_REPORT_CHARS = 2000
_MAX_REGRESSIONS = 20
_DEFAULT_STATUS_ENTITY = "sensor.upgrade_advisor_status"
_DEFAULT_RISK_ENTITY = "sensor.upgrade_advisor_risk_level"

# Seconds between poll attempts while waiting for the advisor to finish.
_POLL_INTERVAL_SECONDS = 10

# Unique-ID suffixes used by upstream to identify the two entities.
_STATUS_SUFFIX = "_status"
_RISK_SUFFIX = "_risk_level"

# Component types that the advisor supports (case-insensitive prefix match).
# Add-ons are not supported upstream; skip them.
_CORE_COMPONENTS = {"homeassistant", "core"}
_HACS_SERVICE = "analyze"
_CORE_SERVICE = "analyze_version"


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
    last_post_upgrade_check: Optional[str] = None  # ISO datetime string from the sensor


async def _resolve_entity_ids(
    ws_client: "HAWebSocketClientProtocol",
) -> Tuple[str, str]:
    """Return (status_entity_id, risk_entity_id) from the entity registry.

    Queries config/entity_registry/list for platform == "upgrade_advisor" and
    identifies each entity by its unique_id suffix.  Falls back to the module
    defaults if the registry call fails or the entities are not found.
    """
    try:
        entries = await ws_client.get_entity_registry()
    except Exception:  # nosec B110
        return _DEFAULT_STATUS_ENTITY, _DEFAULT_RISK_ENTITY

    status_id: Optional[str] = None
    risk_id: Optional[str] = None
    for entry in entries:
        if entry.get("platform") != "upgrade_advisor":
            continue
        uid = str(entry.get("unique_id") or "")
        entity_id = str(entry.get("entity_id") or "")
        if not entity_id:
            continue
        if uid.endswith(_STATUS_SUFFIX):
            status_id = entity_id
        elif uid.endswith(_RISK_SUFFIX):
            risk_id = entity_id

    return (
        status_id or _DEFAULT_STATUS_ENTITY,
        risk_id or _DEFAULT_RISK_ENTITY,
    )


def _extract_component_section(report: str, component: str) -> str:
    """Extract the markdown section for component from the advisor report.

    Looks for a heading line containing the component name and returns from
    that heading to the next heading at the same or higher level.  Returns
    the full report when no matching section is found.
    """
    comp_lower = component.lower()
    lines = report.splitlines()
    start = None
    heading_level = 1
    for i, line in enumerate(lines):
        if line.startswith("#") and comp_lower in line.lower():
            start = i
            heading_level = len(line) - len(line.lstrip("#"))
            break
    if start is None:
        return report
    section: List[str] = [lines[start]]
    for line in lines[start + 1 :]:
        if (
            line.startswith("#")
            and (len(line) - len(line.lstrip("#"))) <= heading_level
        ):
            break
        section.append(line)
    return "\n".join(section)


def _parse_iso_timestamp(value: str) -> Optional[float]:
    """Parse an ISO 8601 datetime string to a UTC epoch float.  Returns None on failure."""
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


async def read_advisor_report(
    rest: "HARestClientProtocol",
    target_version: str,
    *,
    ws_client: Optional["HAWebSocketClientProtocol"] = None,
    component_type: str = "",
    trigger_time: Optional[float] = None,
) -> Optional[AdvisorReport]:
    """Return an AdvisorReport when a ready, matching report exists.

    For Core updates (component_type in _CORE_COMPONENTS or empty):
      - available_version must equal target_version.
    For HACS component updates:
      - available_version is the Core version and is not checked.
      - When trigger_time (epoch float) is provided, last_analysis must be newer
        than the trigger to avoid returning a stale pre-existing report.
      - The component's section is extracted from the full report text.
    Returns None when the integration is absent, the sensor is not in
    "report_ready" state, or the freshness check fails.
    """
    if ws_client is not None:
        status_entity_id, risk_entity_id = await _resolve_entity_ids(ws_client)
    else:
        status_entity_id, risk_entity_id = _DEFAULT_STATUS_ENTITY, _DEFAULT_RISK_ENTITY

    try:
        status_entity = await rest.get_state(status_entity_id)
    except Exception:  # nosec B110 — not installed; skip silently
        return None

    state = status_entity.get("state", "")
    if state != "report_ready":
        return None

    attrs = status_entity.get("attributes") or {}
    available_version = attrs.get("available_version", "")

    _is_core = not component_type or component_type.lower() in _CORE_COMPONENTS
    if _is_core:
        if available_version != target_version:
            return None
    else:
        # For HACS components, available_version reflects the Core version — skip it.
        # When we triggered the analysis, require the report to be newer.
        if trigger_time is not None:
            last_analysis_str = str(attrs.get("last_analysis") or "").strip()
            if last_analysis_str:
                la_ts = _parse_iso_timestamp(last_analysis_str)
                if la_ts is not None and la_ts < trigger_time:
                    log.debug(
                        "advisor_report_predates_trigger",
                        last_analysis=last_analysis_str,
                        component=component_type,
                    )
                    return None

    report = str(attrs.get("report") or "")
    if not _is_core and component_type and report:
        report = _extract_component_section(report, component_type)
    if len(report) > _MAX_REPORT_CHARS:
        report = report[:_MAX_REPORT_CHARS] + "...[truncated]"

    raw_count = attrs.get("breaking_change_count", 0)
    try:
        breaking_change_count = int(raw_count)
    except (TypeError, ValueError):
        breaking_change_count = 0

    risk = "unknown"
    try:
        risk_entity = await rest.get_state(risk_entity_id)
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


async def request_advisor_analysis(
    rest: "HARestClientProtocol",
    version: str,
    component_type: str,
    *,
    ws_client: Optional["HAWebSocketClientProtocol"] = None,
    timeout_seconds: float = 1800.0,
) -> None:
    """Trigger an advisor analysis and wait until the sensor leaves "analyzing".

    Calls upgrade_advisor.analyze_version for Core/HA updates or
    upgrade_advisor.analyze for HACS component updates.  Add-ons are not
    supported by upstream and are silently skipped.

    After this coroutine returns, callers should call read_advisor_report() to
    read the result.  Returns immediately (without error) when the integration
    is absent or the service call fails.

    Args:
        rest: REST client for state reads and service calls.
        version: The target version string (used as the payload for analyze_version).
        component_type: Component identifier, e.g. "homeassistant", "core", or a
            HACS integration slug.  Add-ons (anything not in _CORE_COMPONENTS and not
            starting with a lowercase slug used by HACS) are skipped.
        ws_client: Optional WebSocket client for entity-registry resolution.
        timeout_seconds: Maximum seconds to wait for the sensor to leave "analyzing".
    """
    comp_lower = component_type.lower()

    if comp_lower in _CORE_COMPONENTS:
        service = _CORE_SERVICE
        payload: dict = {"version": version}
    elif comp_lower in {"supervisor", "os", "haos"}:
        # Upstream does not support Supervisor/OS analysis
        return
    else:
        service = _HACS_SERVICE
        payload = {}

    try:
        await rest.call_service("upgrade_advisor", service, payload)
    except Exception as exc:  # nosec B110
        log.debug(
            "advisor_trigger_failed",
            service=service,
            component=component_type,
            exc=str(exc),
        )
        return

    log.info(
        "advisor_analysis_triggered",
        service=service,
        component=component_type,
        version=version,
    )

    if ws_client is not None:
        status_entity_id, _ = await _resolve_entity_ids(ws_client)
    else:
        status_entity_id = _DEFAULT_STATUS_ENTITY

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        try:
            entity = await rest.get_state(status_entity_id)
            state = entity.get("state", "")
        except Exception:  # nosec B110
            break
        if state != "analyzing":
            log.info(
                "advisor_analysis_complete",
                state=state,
                component=component_type,
            )
            return

    log.warning(
        "advisor_analysis_timeout",
        component=component_type,
        timeout_seconds=timeout_seconds,
    )


async def read_post_upgrade_report(
    rest: "HARestClientProtocol",
    *,
    update_completed_at: Optional[float] = None,
) -> Optional[PostUpgradeReport]:
    """Return post-upgrade status and regressions from the advisor sensor.

    Reads sensor.upgrade_advisor_status attributes post_upgrade_status and
    post_upgrade_regressions.  Returns None when the integration is absent,
    neither attribute is populated, or the report's last_post_upgrade_check
    timestamp predates update_completed_at (stale from a previous update).
    """
    try:
        status_entity = await rest.get_state(_DEFAULT_STATUS_ENTITY)
    except Exception:  # nosec B110 — not installed; skip silently
        return None

    attrs = status_entity.get("attributes") or {}
    post_status = str(attrs.get("post_upgrade_status") or "").strip()
    raw_regressions = attrs.get("post_upgrade_regressions") or []

    if not post_status and not raw_regressions:
        return None

    # Require the report to be newer than the update when a completion time is given.
    last_check_str = str(attrs.get("last_post_upgrade_check") or "").strip()
    if update_completed_at is not None and last_check_str:
        lc_ts = _parse_iso_timestamp(last_check_str)
        if lc_ts is not None and lc_ts < update_completed_at:
            log.debug(
                "post_upgrade_report_stale",
                last_post_upgrade_check=last_check_str,
            )
            return None

    if isinstance(raw_regressions, str):
        regressions = [raw_regressions] if raw_regressions.strip() else []
    else:
        regressions = [str(r) for r in list(raw_regressions)][:_MAX_REGRESSIONS]

    return PostUpgradeReport(
        post_upgrade_status=post_status,
        regressions=regressions,
        last_post_upgrade_check=last_check_str or None,
    )
