"""Pueo self-diagnostics — structured gap report comparing actual vs. intended state."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol
    from netalertx.api_client import NetAlertXAPIClient

_STATUS_ORDER = {"CRITICAL": 0, "WARN": 1, "OK": 2}


@dataclass
class AuditResult:
    name: str
    status: str  # "OK" | "WARN" | "CRITICAL"
    detail: str
    action: str = field(default="")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_service() -> AuditResult:
    """Check whether the launchd service is installed and running."""
    from utils.service import service_status

    st = service_status()
    if st.get("error"):
        return AuditResult(
            "launchd_service",
            "WARN",
            f"launchctl unavailable: {st['error']}",
            "Run on macOS to enable launchd service management",
        )
    if not st["loaded"]:
        return AuditResult(
            "launchd_service",
            "WARN",
            "Pueo launchd service not installed",
            "Run: python main.py --mode install-service",
        )
    if not st["running"]:
        return AuditResult(
            "launchd_service",
            "CRITICAL",
            "Service installed but not running (no PID)",
            "Run: launchctl start com.pueo.agent",
        )
    return AuditResult(
        "launchd_service",
        "OK",
        f"Service running (PID {st['pid']})",
    )


async def check_ha_disk(
    ssh_client: Optional["SSHClientProtocol"] = None,
) -> AuditResult:
    """Check HA host disk free vs. configured critical/warn thresholds."""
    import config
    from utils.resource import poll_host_resources
    from utils.ha.ssh_client import AsyncSSHClient

    try:
        client: "SSHClientProtocol" = ssh_client or AsyncSSHClient(
            config.HA_HOST, config.HA_USER, config.SSH_KEY_PATH
        )
        status = await poll_host_resources(
            client,
            config.HA_DISK_WARN_GB,
            config.HA_DISK_CRITICAL_GB,
            config.HA_MEM_WARN_MB,
        )
    except Exception as exc:
        return AuditResult(
            "ha_disk",
            "WARN",
            f"SSH unavailable — disk check skipped ({exc})",
            "Check SSH connectivity to HA host",
        )

    used_pct = (
        100 * status.disk_used_gb / status.disk_total_gb if status.disk_total_gb else 0
    )
    detail = (
        f"{status.disk_free_gb:.1f} GB free / {status.disk_total_gb:.1f} GB total "
        f"({used_pct:.0f}% used)"
    )

    if status.disk_critical:
        return AuditResult(
            "ha_disk",
            "CRITICAL",
            detail,
            f"Free disk immediately; critical threshold is {config.HA_DISK_CRITICAL_GB} GB",
        )
    if status.disk_warn:
        return AuditResult(
            "ha_disk",
            "WARN",
            detail,
            f"Monitor disk usage; warn threshold is {config.HA_DISK_WARN_GB} GB",
        )
    return AuditResult("ha_disk", "OK", detail)


async def check_backup_registry(
    ssh_client: Optional["SSHClientProtocol"] = None,
) -> AuditResult:
    """Compare backup_registry DB entries against actual HA backups via SSH."""
    import config
    from agents.ha_agent_advanced import list_ha_backups
    from utils.ha.ssh_client import AsyncSSHClient

    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT backup_slug, location FROM backup_registry"
            ).fetchall()
    except sqlite3.OperationalError:
        return AuditResult(
            "backup_registry",
            "WARN",
            "backup_registry table missing (DB not initialized)",
            "Run init_local_database() or start any agent mode once",
        )

    db_slugs = {r[0]: r[1] for r in rows}
    unknown_count = sum(1 for s in db_slugs if s == "unknown_slug")
    real_db_slugs = {s for s in db_slugs if s != "unknown_slug"}

    try:
        client: "SSHClientProtocol" = ssh_client or AsyncSSHClient(
            config.HA_HOST, config.HA_USER, config.SSH_KEY_PATH
        )
        ha_backups = await list_ha_backups(ssh_client=client)
        ha_slugs = {b["slug"] for b in ha_backups}
    except Exception as exc:
        parts = [f"{len(db_slugs)} tracked in DB"]
        if unknown_count:
            parts.append(f"{unknown_count} 'unknown_slug' placeholder(s)")
        parts.append(f"SSH unavailable for HA comparison ({exc})")
        return AuditResult(
            "backup_registry",
            "WARN",
            "; ".join(parts),
            "Check SSH connectivity and run reconcile",
        )

    untracked = ha_slugs - real_db_slugs
    orphaned = real_db_slugs - ha_slugs

    parts = [f"{len(ha_slugs)} HA backup(s), {len(db_slugs)} tracked in DB"]
    if unknown_count:
        parts.append(f"{unknown_count} 'unknown_slug' placeholder(s)")
    if untracked:
        parts.append(f"{len(untracked)} HA backup(s) not in DB (reconciliation needed)")
    if orphaned:
        parts.append(f"{len(orphaned)} DB entry/entries orphaned (not on HA)")

    detail = "; ".join(parts)

    if untracked or orphaned or unknown_count:
        return AuditResult(
            "backup_registry",
            "WARN",
            detail,
            "Run --mode backup-status or trigger reconcile_backup_inventory()",
        )
    return AuditResult("backup_registry", "OK", detail)


def check_pending_hitl(watch_dir: str = "hitl/") -> AuditResult:
    """Report approval cards in the watch dir that have no .approved or .rejected file."""
    hitl_path = Path(watch_dir)
    if not hitl_path.exists():
        return AuditResult(
            "pending_hitl",
            "OK",
            "Approval queue directory does not exist; no pending cards",
        )

    pending: list[tuple[str, float]] = []
    for jf in hitl_path.glob("*.json"):
        nid = jf.stem
        if (hitl_path / f"{nid}.approved").exists() or (
            hitl_path / f"{nid}.rejected"
        ).exists():
            continue
        try:
            data = json.loads(jf.read_text())
            sent_at = float(data.get("sent_at", jf.stat().st_mtime))
        except Exception:
            sent_at = jf.stat().st_mtime
        pending.append((jf.name, sent_at))

    if not pending:
        return AuditResult("pending_hitl", "OK", "No pending approval cards")

    now = time.time()
    oldest_age_hours = max((now - sent_at) / 3600 for _, sent_at in pending)
    detail = f"{len(pending)} pending card(s); oldest is {oldest_age_hours:.0f}h old"
    action = "Open the Pueo dashboard and approve or reject pending actions"

    if oldest_age_hours > 24:
        return AuditResult("pending_hitl", "CRITICAL", detail, action)
    return AuditResult("pending_hitl", "WARN", detail, action)


async def check_netalertx(
    api_client: Optional["NetAlertXAPIClient"] = None,
) -> AuditResult:
    """Check NetAlertX scan freshness, device count, and MQTT status."""
    import config

    if not config.NETALERTX_HOST:
        return AuditResult(
            "netalertx",
            "OK",
            "NetAlertX not configured (netalertx_host not set)",
        )

    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.health import NetAlertXHealthMonitor

    try:
        client = api_client or NetAlertXAPIClient(
            base_url=f"http://{config.NETALERTX_HOST}:{config.NETALERTX_API_PORT}",
            api_token=config.NETALERTX_API_TOKEN,
        )
        monitor = NetAlertXHealthMonitor(
            api_client=client,
            max_scan_age_minutes=config.NETALERTX_MAX_SCAN_AGE_MINUTES,
        )
        empty_q: asyncio.Queue = asyncio.Queue()
        report = await monitor.poll_once(event_queue=empty_q)
    except Exception as exc:
        return AuditResult(
            "netalertx",
            "WARN",
            f"NetAlertX API unavailable ({exc})",
            "Check NetAlertX service and connectivity",
        )

    total = report.device_counts.get("total", 0)
    online = report.device_counts.get("online", 0)
    scan_age = report.last_scan_age_minutes
    mqtt_status = "MQTT active" if report.mqtt_active else "MQTT inactive"

    parts = [
        f"scan age {scan_age}m",
        f"{total} devices ({online} online)",
        mqtt_status,
    ]

    if scan_age > config.NETALERTX_MAX_SCAN_AGE_MINUTES:
        return AuditResult(
            "netalertx",
            "CRITICAL",
            "; ".join(parts),
            f"Trigger a NetAlertX scan; max age is {config.NETALERTX_MAX_SCAN_AGE_MINUTES}m",
        )
    if not report.mqtt_active:
        return AuditResult(
            "netalertx",
            "WARN",
            "; ".join(parts),
            "Check MQTT bridge configuration in NetAlertX",
        )
    return AuditResult("netalertx", "OK", "; ".join(parts))


def check_netalertx_api_token() -> AuditResult:
    """Warn when NetAlertX setup is desired but no API token has been configured."""
    import config as _cfg

    if not _cfg.NETALERTX_SETUP_DESIRED:
        return AuditResult(
            "netalertx_api_token",
            "OK",
            "NetAlertX setup not enabled (netalertx.setup_desired=false)",
        )
    if not _cfg.NETALERTX_API_TOKEN:
        return AuditResult(
            "netalertx_api_token",
            "WARN",
            "netalertx.setup_desired=true but api_token is empty",
            "Generate token: NetAlertX UI → Settings → Main Settings → API Key, "
            "then set netalertx.api_token in config.yaml and restart Pueo",
        )
    return AuditResult("netalertx_api_token", "OK", "NetAlertX API token is configured")


def check_state_history() -> AuditResult:
    """Report the ratio of is_valid=0 in state_history and flag suspicious patterns."""
    import config

    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            total = conn.execute("SELECT COUNT(*) FROM state_history").fetchone()[0]
            invalid = conn.execute(
                "SELECT COUNT(*) FROM state_history WHERE is_valid = 0"
            ).fetchone()[0]
    except sqlite3.OperationalError:
        return AuditResult(
            "state_history",
            "WARN",
            "state_history table missing (DB not initialized)",
            "Run init_local_database() or start any agent mode once",
        )

    if total == 0:
        return AuditResult(
            "state_history",
            "OK",
            "No state history entries (agent has not run a diagnose cycle yet)",
        )

    invalid_pct = 100 * invalid / total
    detail = f"{total} entries, {invalid_pct:.0f}% flagged is_valid=0"

    if invalid == total and total > 0:
        return AuditResult(
            "state_history",
            "WARN",
            detail + " — all runs flagged invalid; may be test data in production DB",
            "Verify DB_PATH is not shared between test and production runs",
        )
    return AuditResult("state_history", "OK", detail)


def check_update_check(watch_dir: str = "hitl/") -> AuditResult:
    """Report update loop config, pending update cards, and approved-but-unexecuted updates."""
    import config

    parts: list[str] = []
    if config.HA_UPDATE_CHECK_INTERVAL_HOURS == 0:
        parts.append("update_check_interval_hours=0 (loop disabled)")
    else:
        parts.append(f"check every {config.HA_UPDATE_CHECK_INTERVAL_HOURS}h")

    if not config.HA_API_TOKEN:
        parts.append("ha_api_token not set")

    hitl_path = Path(watch_dir)
    pending_updates: list[str] = []
    approved_not_executed: list[str] = []

    if hitl_path.exists():
        for jf in hitl_path.glob("*.json"):
            try:
                data = json.loads(jf.read_text())
            except Exception:  # nosec B112 — intentionally skip malformed card JSON
                continue
            nid = jf.stem
            subject = data.get("subject", "")
            card_type = data.get("card_type") or data.get("payload", {}).get(
                "card_type", ""
            )
            is_update_card = card_type == "update" or (
                not card_type and ("update available" in subject.lower())
            )
            if not is_update_card:
                continue

            approved = (hitl_path / f"{nid}.approved").exists()
            rejected = (hitl_path / f"{nid}.rejected").exists()
            if rejected:
                continue
            if approved:
                if not data.get("fix_applied"):
                    approved_not_executed.append(subject or nid)
            else:
                pending_updates.append(subject or nid)

    if approved_not_executed:
        parts.append(
            f"{len(approved_not_executed)} approved update(s) not yet executed: "
            + "; ".join(approved_not_executed)
        )
        return AuditResult(
            "update_check",
            "CRITICAL",
            "; ".join(parts),
            "Start the supervisor (python main.py) so the dispatch handler can execute the update",
        )
    if pending_updates:
        parts.append(
            f"{len(pending_updates)} update(s) awaiting approval: "
            + "; ".join(pending_updates)
        )
        return AuditResult(
            "update_check",
            "WARN",
            "; ".join(parts),
            "Open the Pueo dashboard to approve or reject the pending update card(s)",
        )
    if config.HA_UPDATE_CHECK_INTERVAL_HOURS == 0 or not config.HA_API_TOKEN:
        return AuditResult(
            "update_check",
            "WARN",
            "; ".join(parts),
            "Set update_check_interval_hours > 0 and ha_api_token to enable update monitoring",
        )
    return AuditResult("update_check", "OK", "; ".join(parts))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_audit(
    ssh_client: Optional["SSHClientProtocol"] = None,
    netalertx_client: Optional["NetAlertXAPIClient"] = None,
    watch_dir: str = "hitl/",
) -> list[AuditResult]:
    """Run all audit checks; return results sorted CRITICAL → WARN → OK."""
    results: list[AuditResult] = []

    # Sync checks — run first (fast, no I/O)
    results.append(check_service())
    results.append(check_state_history())
    results.append(check_pending_hitl(watch_dir=watch_dir))
    results.append(check_update_check(watch_dir=watch_dir))
    results.append(check_netalertx_api_token())

    # Async checks (SSH / external API) — gather in parallel, tolerate per-check failures
    async_tasks = [
        check_ha_disk(ssh_client=ssh_client),
        check_backup_registry(ssh_client=ssh_client),
        check_netalertx(api_client=netalertx_client),
    ]
    async_results = await asyncio.gather(*async_tasks, return_exceptions=True)
    for res in async_results:
        if isinstance(res, AuditResult):
            results.append(res)
        else:
            # Unexpected exception from a check — surface as WARN
            results.append(
                AuditResult(
                    "unknown_check",
                    "WARN",
                    f"Check raised unexpected exception: {res}",
                    "Investigate the error above",
                )
            )

    results.sort(key=lambda r: _STATUS_ORDER.get(r.status, 99))
    return results


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_audit_report(results: list[AuditResult], now: float | None = None) -> str:
    """Render audit results as a markdown report."""
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now or time.time()))
    lines: list[str] = [f"# Pueo Self-Audit — {ts}", ""]

    for r in results:
        badge = f"[{r.status}]"
        lines.append(f"## {badge} {r.name.replace('_', ' ').title()}")
        lines.append(f"{r.detail}")
        if r.action:
            lines.append(f"**Action:** {r.action}")
        lines.append("")

    critical = [r for r in results if r.status == "CRITICAL"]
    warn = [r for r in results if r.status == "WARN"]

    if critical or warn:
        lines.append("## Priority Actions")
        lines.append("")
        priority_items = critical + warn
        for i, r in enumerate(priority_items, 1):
            lines.append(f"{i}. **[{r.status}] {r.name}** — {r.action or r.detail}")
        lines.append("")

    return "\n".join(lines)


def save_audit_report(report: str, audits_dir: str = "audits/") -> Path:
    """Write the report to audits/pueo-audit-<date>.md; return the path."""
    audit_path = Path(audits_dir)
    audit_path.mkdir(parents=True, exist_ok=True)
    date_str = time.strftime("%Y-%m-%d")
    out_file = audit_path / f"pueo-audit-{date_str}.md"
    out_file.write_text(report, encoding="utf-8")
    return out_file


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main_audit(watch_dir: str = "hitl/", audits_dir: str = "audits/") -> None:
    """Run the audit and print a summary to stdout; save the full report to audits/."""
    results = await run_audit(watch_dir=watch_dir)
    report = format_audit_report(results)
    out_path = save_audit_report(report, audits_dir=audits_dir)

    # Print summary to stdout
    critical = [r for r in results if r.status == "CRITICAL"]
    warn = [r for r in results if r.status == "WARN"]
    ok = [r for r in results if r.status == "OK"]

    print(f"\nPueo Audit — {len(results)} checks")
    print(f"  CRITICAL: {len(critical)}  WARN: {len(warn)}  OK: {len(ok)}")

    for r in results:
        icon = {"CRITICAL": "✘", "WARN": "⚠", "OK": "✓"}.get(r.status, "?")
        print(f"  {icon} [{r.status}] {r.name}: {r.detail}")
        if r.action and r.status != "OK":
            print(f"      → {r.action}")

    print(f"\nFull report saved to {out_path}")
