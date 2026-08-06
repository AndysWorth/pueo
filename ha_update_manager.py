#!/usr/bin/env python3
"""HA Update Manager — detects available updates and surfaces them as HITL cards."""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import httpx
from pydantic import BaseModel

from config import (
    AUTONOMY_LEVEL,
    HA_API_PORT,
    HA_API_TOKEN,
    HA_DISK_CRITICAL_GB,
    HA_DISK_WARN_GB,
    HA_HOST,
    HA_MEM_WARN_MB,
    HA_UPDATE_RELEASE_NOTES_CACHE_DIR,
    MAX_PROMPT_TOKENS,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    OLLAMA_MODEL,
    SSH_KEY_PATH,
    HA_USER,
)
from interfaces import HARestClientProtocol, LLMClientProtocol, SSHClientProtocol
from utils.autonomy import RiskLevel
from utils.context import truncate_to_budget
from utils.ha_rest_client import HARestClient, UpdateStatus, get_update_status
from utils.logging import get_logger

if TYPE_CHECKING:
    from utils.autonomy import AutonomyGate, FakeAutonomyGate
    from utils.ha_environment import HAEnvironmentProfile
    from utils.notify import NotifierProtocol

log = get_logger("ha_update_manager")

# SSH commands Pueo uses — checked against release notes for rename/removal risk.
PUEO_SSH_COMMANDS = [
    "ha core check",
    "ha core restart",
    "ha core update",
    "ha core info --raw-json",
    "ha core logs",
    "ha apps list",
    "ha apps info <slug>",
    "ha backup new",
    "ha os update",
    "ha os info",
    "ha host info",
]

_GITHUB_API_URL = (
    "https://api.github.com/repos/home-assistant/core/releases/tags/{version}"
)


class UpdateReadinessReport(BaseModel):
    target_version: str
    safe_to_update: bool  # advisory only
    breaking_changes: list[str]
    affected_config_keys: list[str]
    pueo_command_risks: list[str]
    recommendation: str


async def _fetch_github_release_notes(version: str) -> str:  # pragma: no cover
    """Fetch release notes body from the GitHub API for a given HA Core version."""
    url = _GITHUB_API_URL.format(version=version)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url,
            headers={"Accept": "application/vnd.github.v3+json"},
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("body") or "No release notes available."


async def fetch_release_notes_cached(
    version: str,
    cache_dir: str,
    *,
    _fetcher=None,  # injectable for tests
) -> str:
    """Return cached release notes; fetch from GitHub API if not yet cached.

    GA monthly releases often have a blog-URL stub body (<500 chars).  When
    detected, tries beta tags (b5→b0) for the same minor version to get the
    real changelog content.
    """
    cache_path = Path(cache_dir) / f"{version}.txt"
    if cache_path.exists():
        return cache_path.read_text()
    fetcher = _fetcher or _fetch_github_release_notes
    notes = await fetcher(version)
    if len(notes.strip()) < 500:
        parts = version.split(".")
        if len(parts) >= 2:
            minor_base = f"{parts[0]}.{parts[1]}.0"
            for beta_num in range(5, -1, -1):
                try:
                    beta_notes = await fetcher(f"{minor_base}b{beta_num}")
                    if len(beta_notes.strip()) >= 500:
                        notes = beta_notes
                        break
                except Exception:  # nosec B112
                    continue
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(notes)
    return notes


async def analyze_breaking_changes(
    update_status: UpdateStatus,
    release_notes: str,
    llm_client: Optional[LLMClientProtocol] = None,
    profile: Optional["HAEnvironmentProfile"] = None,
) -> UpdateReadinessReport:
    """LLM advisory analysis of release notes against the current installation.

    When profile is provided, a structured installation summary is used in the
    prompt instead of raw configuration YAML.  When profile is None, the prompt
    omits installation context.
    """
    if len(release_notes.strip()) < 500:
        url_match = re.search(r"https?://\S+", release_notes)
        url_hint = (
            url_match.group(0) if url_match else "https://www.home-assistant.io/blog/"
        )
        return UpdateReadinessReport(
            target_version=update_status.latest_version,
            safe_to_update=True,
            breaking_changes=[],
            affected_config_keys=[],
            pueo_command_risks=[],
            recommendation=(
                f"Release notes for {update_status.latest_version} are not yet "
                f"available on GitHub. Review manually: {url_hint}"
            ),
        )

    from utils.ollama_client import OllamaClient

    client: LLMClientProtocol = llm_client or OllamaClient()  # pragma: no cover

    command_catalog = "\n".join(f"  - {cmd}" for cmd in PUEO_SSH_COMMANDS)
    notes_content = truncate_to_budget(release_notes, MAX_PROMPT_TOKENS - 400)

    if profile is not None:
        domains = ", ".join(profile.installed_integrations) or "unknown"
        keys = ", ".join(profile.config_yaml_top_keys) or "unknown"
        installation_section = (
            f"=== Installed integrations ===\n{domains}\n\n"
            f"HA version: {profile.ha_version}\n"
            f"Top-level config keys: {keys}"
        )
    else:
        installation_section = ""

    user_content_parts = [
        f"Target version: {update_status.latest_version}",
        f"Installed version: {update_status.installed_version}",
    ]
    if installation_section:
        user_content_parts.append(f"\n{installation_section}")
    user_content_parts += [
        f"\n=== Release notes ===\n{notes_content}",
        f"\n=== Pueo SSH command catalog ===\n{command_catalog}",
        "\nList breaking changes, affected config keys, and any Pueo commands "
        "that appear in breaking changes or migration notes.",
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "You are analyzing a Home Assistant update for potential breaking changes. "
                "Your analysis is ADVISORY ONLY — the user makes the final decision. "
                "Review the release notes against the current configuration and Pueo "
                "SSH command catalog. Identify breaking changes, deprecated settings, "
                "and renamed or removed CLI commands that may affect this installation."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(user_content_parts),
        },
    ]

    response = await client.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        options={"temperature": 0.0},
        format=UpdateReadinessReport.model_json_schema(),
    )
    raw = response["message"]["content"]
    return UpdateReadinessReport.model_validate_json(raw)


def _format_update_table(updates: list[UpdateStatus]) -> str:
    if not updates:
        return "No update entities found via REST API."

    header = f"{'Component':<30} {'Installed':<20} {'Latest':<20} {'Available':<10}"
    separator = "-" * len(header)
    rows = [header, separator]
    for u in updates:
        available = "YES" if u.update_available else "no"
        rows.append(
            f"{u.component:<30} {u.installed_version:<20} {u.latest_version:<20} {available:<10}"
        )
    return "\n".join(rows)


def _format_readiness_report(report: UpdateReadinessReport) -> str:
    lines = [
        f"\n=== Breaking Change Analysis: {report.target_version} ===",
        f"Advisory: {'SAFE' if report.safe_to_update else 'REVIEW REQUIRED'}",
        f"Recommendation: {report.recommendation}",
    ]
    if report.breaking_changes:
        lines.append("\nBreaking changes:")
        for bc in report.breaking_changes:
            lines.append(f"  • {bc}")
    if report.affected_config_keys:
        lines.append("\nAffected config keys:")
        for key in report.affected_config_keys:
            lines.append(f"  • {key}")
    if report.pueo_command_risks:
        lines.append("\nPueo command risks:")
        for risk in report.pueo_command_risks:
            lines.append(f"  ⚠ {risk}")
    return "\n".join(lines)


@dataclass
class UpdatePreflight:
    """Result of a pre-update preflight check."""

    disk_before_gb: float
    disk_after_gb: float
    backups_purged_count: int
    disk_ok: bool  # True when disk_after_gb >= HA_DISK_WARN_GB + 1.5
    supervisor_status: str  # "current" | "update_available" | "unknown"
    problems: list = field(default_factory=list)


# Extra disk headroom required beyond HA_DISK_WARN_GB before an update is considered safe.
_PREFLIGHT_DISK_BUFFER_GB = 1.5


async def run_update_preflight(
    ssh_client: Optional[SSHClientProtocol] = None,
    rest_client: Optional[HARestClientProtocol] = None,
    watch_dir: Optional[Path] = None,
) -> UpdatePreflight:
    """Pre-flight check before a Core or OS update HITL card is sent.

    1. Read cached (or live) disk free on the HA host.
    2. If disk is below HA_DISK_WARN_GB + 1.5 GB, run enforce_ha_retention() to delete
       confirmed-offloaded backups from HA, then re-poll disk.
    3. Check whether the Supervisor has a pending update via REST or watch-dir scan.
    Returns a structured summary that is embedded in the update HITL card body.
    """
    from ha_agent_advanced import enforce_ha_retention
    from utils.resource import get_resource_status, poll_host_resources
    from utils.ssh_client import AsyncSSHClient

    disk_threshold = HA_DISK_WARN_GB + _PREFLIGHT_DISK_BUFFER_GB

    # --- Step 1: get current disk reading ---
    cached = get_resource_status()
    if cached is not None:
        disk_before_gb = cached.disk_free_gb
    else:
        ssh = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
        status = await poll_host_resources(
            ssh, HA_DISK_WARN_GB, HA_DISK_CRITICAL_GB, HA_MEM_WARN_MB
        )
        disk_before_gb = status.disk_free_gb

    problems: list[str] = []
    purged = 0
    disk_after_gb = disk_before_gb

    # --- Step 2: enforce retention if disk is low ---
    if disk_before_gb < disk_threshold:
        ssh = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
        purged = await enforce_ha_retention(ssh_client=ssh)
        if purged == 0:
            problems.append(
                "No confirmed-offloaded backups available to delete — "
                "free disk manually before updating"
            )
        # Re-poll after enforcement to get a fresh reading
        fresh = await poll_host_resources(
            ssh, HA_DISK_WARN_GB, HA_DISK_CRITICAL_GB, HA_MEM_WARN_MB
        )
        disk_after_gb = fresh.disk_free_gb
        if disk_after_gb < disk_threshold:
            problems.append(
                f"Disk still at {disk_after_gb:.1f} GB after cleanup — "
                "update may not have enough space"
            )

    disk_ok = disk_after_gb >= disk_threshold

    # --- Step 3: check Supervisor update status ---
    supervisor_status = "unknown"
    try:
        if rest_client is not None:
            updates = await get_update_status(rest_client)
            sup_updates = [
                u for u in updates if "supervisor" in u.entity_id and u.update_available
            ]
            if sup_updates:
                supervisor_status = "update_available"
                problems.append(
                    "Supervisor update available — approve Supervisor before Core"
                )
            else:
                supervisor_status = "current"
        else:
            # Fallback: scan watch-dir for a pending Supervisor update card
            _watch_dir = watch_dir or Path(NOTIFY_WATCH_DIR)
            pending = _pending_higher_priority_components("core", _watch_dir)
            if "supervisor" in pending:
                supervisor_status = "update_available"
                problems.append(
                    "Supervisor update pending — approve Supervisor before Core"
                )
            else:
                supervisor_status = "current"
    except Exception as exc:
        log.warning("update_preflight_supervisor_check_failed", error=str(exc))
        supervisor_status = "unknown"

    log.info(
        "update_preflight_complete",
        disk_before_gb=round(disk_before_gb, 2),
        disk_after_gb=round(disk_after_gb, 2),
        backups_purged=purged,
        disk_ok=disk_ok,
        supervisor_status=supervisor_status,
        problems=problems,
    )
    return UpdatePreflight(
        disk_before_gb=disk_before_gb,
        disk_after_gb=disk_after_gb,
        backups_purged_count=purged,
        disk_ok=disk_ok,
        supervisor_status=supervisor_status,
        problems=problems,
    )


def _component_risk_level(component: str) -> RiskLevel:
    """Return the risk level for a component update.

    Core and OS updates always require HITL approval (CRITICAL).
    Supervisor updates are HIGH risk.
    Add-on updates are MEDIUM risk and may auto-execute at autonomy level 4.
    """
    if component in ("core", "os"):
        return RiskLevel.CRITICAL
    if component == "supervisor":
        return RiskLevel.HIGH
    return RiskLevel.MEDIUM


async def request_update_approval(
    update: UpdateStatus,
    gate: "AutonomyGate | FakeAutonomyGate",
    notifier: "NotifierProtocol",
    readiness_report: Optional[UpdateReadinessReport] = None,
    disk_free_gb: Optional[float] = None,
    disk_warn: bool = False,
    disk_critical: bool = False,
    notification_id: Optional[str] = None,
    watch_dir: Optional[Path] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    rest_client: Optional[HARestClientProtocol] = None,
) -> bool:
    """Send a per-component HITL update approval card and wait for a decision.

    Returns True if the update should proceed, False if rejected or deferred.
    Risk is always CRITICAL for core/os, HIGH for supervisor, MEDIUM for add-ons.
    For Core and OS updates, runs a pre-flight check (disk clearance + Supervisor status)
    and embeds the result in the card body.
    """
    risk = _component_risk_level(update.component)
    nid = notification_id or str(uuid.uuid4())

    subject = (
        f"Update available: {update.component} "
        f"{update.installed_version} → {update.latest_version}"
    )
    body_parts = [f"Component: {update.component}", f"Risk: {risk.name}"]
    if update.release_summary:
        body_parts.append(f"Summary: {update.release_summary}")
    if readiness_report:
        advisory = "SAFE" if readiness_report.safe_to_update else "REVIEW REQUIRED"
        body_parts.append(f"Advisory: {advisory} — {readiness_report.recommendation}")

    _watch_dir = watch_dir or Path(NOTIFY_WATCH_DIR)
    pending_higher = _pending_higher_priority_components(update.component, _watch_dir)
    if pending_higher:
        body_parts.append(
            f"⚠️ Apply {', '.join(pending_higher)} update(s) first — "
            "correct order: OS → Supervisor → Core → Add-ons"
        )

    # Run pre-flight for Core and OS updates: disk clearance + Supervisor check.
    preflight: Optional[UpdatePreflight] = None
    if update.component in ("core", "os"):
        try:
            preflight = await run_update_preflight(
                ssh_client=ssh_client,
                rest_client=rest_client,
                watch_dir=_watch_dir,
            )
            if preflight.backups_purged_count > 0:
                body_parts.append(
                    f"Pre-flight: freed space by removing "
                    f"{preflight.backups_purged_count} old backup(s) from HA — "
                    f"disk now {preflight.disk_after_gb:.1f} GB"
                )
            if not preflight.disk_ok:
                body_parts.append(
                    f"⚠️ Disk still low ({preflight.disk_after_gb:.1f} GB) — "
                    "update may not have enough space"
                )
            if (
                preflight.supervisor_status == "update_available"
                and update.component == "core"
            ):
                body_parts.append(
                    "⚠️ Supervisor update available — approve Supervisor before Core"
                )
        except Exception as exc:
            log.warning("update_preflight_failed", error=str(exc))

    body = "\n".join(body_parts)

    from utils.card_types import CARD_TYPE_UPDATE

    payload: dict = {
        "notification_id": nid,
        "card_type": CARD_TYPE_UPDATE,
        "component": update.component,
        "entity_id": update.entity_id,
        "installed_version": update.installed_version,
        "latest_version": update.latest_version,
        "release_url": update.release_url,
        "release_summary": update.release_summary,
        "risk": risk.name,
        "severity": risk.name,
        "breaking_changes": (
            readiness_report.breaking_changes if readiness_report else []
        ),
        "affected_config_keys": (
            readiness_report.affected_config_keys if readiness_report else []
        ),
        "pueo_command_risks": (
            readiness_report.pueo_command_risks if readiness_report else []
        ),
        "advisory": readiness_report.recommendation if readiness_report else None,
        "safe_to_update": readiness_report.safe_to_update if readiness_report else None,
        "disk_free_gb": disk_free_gb,
        "disk_warn": disk_warn,
        "disk_critical": disk_critical,
        "preflight_disk_before_gb": preflight.disk_before_gb if preflight else None,
        "preflight_disk_after_gb": preflight.disk_after_gb if preflight else None,
        "preflight_backups_purged": preflight.backups_purged_count if preflight else 0,
        "preflight_disk_ok": preflight.disk_ok if preflight else None,
        "preflight_supervisor_status": (
            preflight.supervisor_status if preflight else None
        ),
        "preflight_problems": preflight.problems if preflight else [],
    }

    log.info(
        "update_hitl_card_sent",
        component=update.component,
        version=update.latest_version,
        risk=risk.name,
        notification_id=nid,
    )
    return await gate.require_approval(subject, body, payload, notifier, risk)


async def run_update_check(
    ha_rest_client: Optional[HARestClientProtocol] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    cache_dir: Optional[str] = None,
    gate: Optional["AutonomyGate | FakeAutonomyGate"] = None,
    notifier: Optional["NotifierProtocol"] = None,
) -> list[UpdateStatus]:
    """One-shot: print update status table, advisory breaking-change analysis, and HITL cards."""
    if not ha_rest_client and not HA_API_TOKEN:
        log.error(
            "update_check_no_token",
            detail="Set home_assistant.api_token in config.yaml to use update-check.",
        )
        print(
            "Error: HA_API_TOKEN is not set. "
            "Add api_token under home_assistant in config.yaml."
        )
        return []

    rest: HARestClientProtocol = ha_rest_client or HARestClient(
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )

    try:
        updates = await get_update_status(rest)
    except Exception as exc:
        log.error("update_check_failed", error=str(exc))
        print(f"Error fetching update status: {exc}")
        return []

    print(_format_update_table(updates))

    available = [u for u in updates if u.update_available]
    if available:
        print(f"\n{len(available)} update(s) available.")
        for u in available:
            if u.release_url:
                print(f"  {u.component}: {u.release_url}")

    # Breaking-change analysis for any Core update; collect reports for HITL cards.
    reports: dict[str, UpdateReadinessReport] = {}
    core_updates = [u for u in available if u.component == "core"]
    for core_update in core_updates:
        release_notes = ""
        resolved_cache_dir = cache_dir or HA_UPDATE_RELEASE_NOTES_CACHE_DIR
        try:
            release_notes = await fetch_release_notes_cached(
                core_update.latest_version, resolved_cache_dir
            )
        except Exception as exc:
            log.warning(
                "release_notes_fetch_failed",
                version=core_update.latest_version,
                error=str(exc),
            )
            print(
                f"\nWarning: could not fetch release notes for {core_update.latest_version}: {exc}"
            )

        if not release_notes:
            continue

        try:
            report = await analyze_breaking_changes(
                core_update, release_notes, llm_client
            )
            reports[core_update.component] = report
            print(_format_readiness_report(report))
            log.info(
                "breaking_change_analysis_complete",
                version=core_update.latest_version,
                safe_to_update=report.safe_to_update,
                breaking_changes=len(report.breaking_changes),
            )
        except Exception as exc:
            log.error("breaking_change_analysis_failed", error=str(exc))
            print(f"\nWarning: breaking-change analysis failed: {exc}")

    # HITL approval cards — Core/OS always require approval; add-ons defer to autonomy level.
    from utils.autonomy import AutonomyGate
    from utils.notify import get_notifier

    _gate = gate or AutonomyGate(AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    for update in available:
        readiness = reports.get(update.component)
        await request_update_approval(update, _gate, _notifier, readiness)
        # Execution is handled by the dashboard's _execute_queued_update on approve.
        # run_update_check() only creates the HITL card; it never calls execute_update().

    return updates


# ── Pueo Self-Check (item 37) ─────────────────────────────────────────────────

# NetAlertX add-on slug used in self-check smoke test.
_NETALERTX_SLUG = "db21ed7f_netalertx_fa"
# Disk buffer beyond the warn threshold needed to run the backup smoke test.
_BACKUP_SMOKE_DISK_BUFFER_GB = 2.0


class SelfCheckCommandRisk(BaseModel):
    """LLM output schema for the post-update command catalog cross-reference."""

    command_risks: list[str]
    summary: str


@dataclass
class PueoSelfCheckResult:
    """Results of Pueo's post-Core-update self-check."""

    core_check_ok: bool
    core_info_ok: bool
    apps_list_ok: bool
    netalertx_info_ok: bool
    backup_smoke_ok: Optional[bool]  # None = skipped (disk constrained)
    command_risks: list[str] = field(default_factory=list)

    @property
    def all_commands_ok(self) -> bool:
        return (
            self.core_check_ok
            and self.core_info_ok
            and self.apps_list_ok
            and self.netalertx_info_ok
        )


async def _self_check_llm_cross_reference(
    release_notes: str,
    llm_client: Optional[LLMClientProtocol] = None,
) -> SelfCheckCommandRisk:
    """Ask the LLM which Pueo SSH commands appear in the update's breaking changes."""
    from utils.ollama_client import OllamaClient

    client: LLMClientProtocol = llm_client or OllamaClient()  # pragma: no cover

    catalog = "\n".join(f"  - {cmd}" for cmd in PUEO_SSH_COMMANDS)
    notes_budget = MAX_PROMPT_TOKENS - 300  # 300 for system + catalog text
    notes_content = truncate_to_budget(release_notes, notes_budget)

    messages = [
        {
            "role": "system",
            "content": (
                "You are verifying which SSH commands in Pueo's command catalog "
                "may have been renamed, removed, or changed in a Home Assistant "
                "Core update. Only flag commands that explicitly appear in the "
                "breaking changes or migration notes."
            ),
        },
        {
            "role": "user",
            "content": (
                f"=== Pueo SSH command catalog ===\n{catalog}\n\n"
                f"=== Release notes ===\n{notes_content}\n\n"
                "List any catalog commands that appear in breaking changes or "
                "migration notes. For each, describe what changed."
            ),
        },
    ]

    response = await client.chat(
        model=OLLAMA_MODEL,
        messages=messages,
        options={"temperature": 0.0},
        format=SelfCheckCommandRisk.model_json_schema(),
    )
    raw = response["message"]["content"]
    return SelfCheckCommandRisk.model_validate_json(raw)


async def run_pueo_self_check(
    ssh_client: SSHClientProtocol,
    version: str,
    cache_dir: Optional[str] = None,
    disk_free_gb: Optional[float] = None,
    llm_client: Optional[LLMClientProtocol] = None,
) -> PueoSelfCheckResult:
    """Run CLI smoke tests and LLM command-catalog cross-reference after a Core update."""
    resolved_cache_dir = cache_dir or HA_UPDATE_RELEASE_NOTES_CACHE_DIR

    # ── CLI smoke tests ────────────────────────────────────────────────────────
    core_check_ok = False
    try:
        ec, _, _ = await ssh_client.run("ha core check", check=False)
        core_check_ok = ec == 0
    except Exception as exc:
        log.warning("self_check_core_check_failed", error=str(exc))

    core_info_ok = False
    try:
        ec, stdout, _ = await ssh_client.run("ha core info --raw-json", check=False)
        core_info_ok = ec == 0 and bool(stdout.strip())
    except Exception as exc:
        log.warning("self_check_core_info_failed", error=str(exc))

    apps_list_ok = False
    try:
        ec, _, _ = await ssh_client.run("ha apps list", check=False)
        apps_list_ok = ec == 0
    except Exception as exc:
        log.warning("self_check_apps_list_failed", error=str(exc))

    netalertx_info_ok = False
    try:
        ec, _, _ = await ssh_client.run(f"ha apps info {_NETALERTX_SLUG}", check=False)
        netalertx_info_ok = ec == 0
    except Exception as exc:
        log.warning("self_check_netalertx_info_failed", error=str(exc))

    # ── Optional backup smoke test ─────────────────────────────────────────────
    backup_smoke_ok: Optional[bool] = None
    disk_ok = (
        disk_free_gb is not None
        and disk_free_gb > HA_DISK_WARN_GB + _BACKUP_SMOKE_DISK_BUFFER_GB
    )
    if disk_ok:
        try:
            from ha_agent_advanced import _extract_backup_slug

            _, bk_out, _ = await ssh_client.run(
                'ha backup new --name "pueo_selfcheck_DELETE_ME"', check=False
            )
            slug = _extract_backup_slug(bk_out.strip())
            if slug and slug != "unknown_slug":
                await ssh_client.run(f"ha backup remove {slug}", check=False)
                backup_smoke_ok = True
            else:
                backup_smoke_ok = False
        except Exception as exc:
            log.warning("self_check_backup_smoke_failed", error=str(exc))
            backup_smoke_ok = False

    # ── LLM command-catalog cross-reference ───────────────────────────────────
    command_risks: list[str] = []
    try:
        notes_path = Path(resolved_cache_dir) / f"{version}.txt"
        if notes_path.exists():
            release_notes = notes_path.read_text()
            risk_report = await _self_check_llm_cross_reference(
                release_notes, llm_client
            )
            command_risks = risk_report.command_risks
            log.info(
                "self_check_llm_cross_reference_complete",
                version=version,
                risks_found=len(command_risks),
            )
        else:
            log.info(
                "self_check_llm_cross_reference_skipped",
                reason="no_cached_notes",
                version=version,
            )
    except Exception as exc:
        log.warning("self_check_llm_cross_reference_failed", error=str(exc))

    result = PueoSelfCheckResult(
        core_check_ok=core_check_ok,
        core_info_ok=core_info_ok,
        apps_list_ok=apps_list_ok,
        netalertx_info_ok=netalertx_info_ok,
        backup_smoke_ok=backup_smoke_ok,
        command_risks=command_risks,
    )
    log.info(
        "self_check_complete",
        version=version,
        all_commands_ok=result.all_commands_ok,
        command_risks=len(command_risks),
    )
    return result


# ── Update Execution (item 36) ────────────────────────────────────────────────

_CORE_UPDATE_TIMEOUT = 480  # seconds
_OS_UPDATE_TIMEOUT = 480
_ADDON_UPDATE_TIMEOUT = 180
_POLL_INTERVAL_CORE = 15
_POLL_INTERVAL_OS = 15
_POLL_INTERVAL_ADDON = 10


async def _poll_core_version(
    target_version: str,
    ssh_client: SSHClientProtocol,
    timeout_seconds: int = _CORE_UPDATE_TIMEOUT,
    interval: int = _POLL_INTERVAL_CORE,
    _sleep: Optional[Callable] = None,
) -> bool:
    """Poll `ha core info` every ``interval`` seconds until version matches target or timeout."""
    sleep_fn = _sleep or asyncio.sleep
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            _, stdout, _ = await ssh_client.run("ha core info --raw-json", check=False)
            data = json.loads(stdout)
            info = data.get("data", data)
            current = info.get("version", "")
            update_available = info.get("update_available", True)
            if current == target_version or not update_available:
                return True
        except Exception as exc:
            log.warning("poll_core_version_error", error=str(exc))
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _poll_os_online(
    ha_host: str,
    ha_port: int,
    timeout_seconds: int = _OS_UPDATE_TIMEOUT,
    interval: int = _POLL_INTERVAL_OS,
    _sleep: Optional[Callable] = None,
    _connect: Optional[Callable] = None,
) -> bool:
    """Poll TCP connect to ha_host:ha_port until success or timeout."""
    sleep_fn = _sleep or asyncio.sleep
    connect_fn = _connect or asyncio.open_connection
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            reader, writer = await connect_fn(ha_host, ha_port)
            writer.close()
            await writer.wait_closed()
            return True
        except (
            Exception
        ):  # nosec B110 — intentional: any connection error means HA not yet up
            pass
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _wait_for_ha_down(
    ha_host: str,
    ha_port: int,
    timeout_seconds: int = 60,
    _sleep: Optional[Callable] = None,
    _connect: Optional[Callable] = None,
) -> bool:
    """Poll TCP until connection is refused (HA has started shutting down)."""
    sleep_fn = _sleep or asyncio.sleep
    connect_fn = _connect or asyncio.open_connection
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            reader, writer = await connect_fn(ha_host, ha_port)
            writer.close()
            await writer.wait_closed()
        except (ConnectionRefusedError, OSError):
            return True
        await sleep_fn(2)
    return False


async def _poll_ha_api_ready(
    ha_host: str,
    ha_port: int,
    token: str,
    timeout_seconds: int = 120,
    interval: int = 5,
    _sleep: Optional[Callable] = None,
    _get: Optional[Callable] = None,
) -> bool:
    """Poll GET /api/ until HA HTTP API returns 200 or 401 (API layer is up)."""
    sleep_fn = _sleep or asyncio.sleep
    url = f"http://{ha_host}:{ha_port}/api/"
    headers = {"Authorization": f"Bearer {token}"}
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            if _get is not None:
                status = await _get(url, headers)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=headers)
                    status = resp.status_code
            if status in (200, 401):
                return True
        except Exception:  # nosec B110 — connection errors expected during boot
            pass
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _poll_addon_version(
    slug: str,
    target_version: str,
    ssh_client: SSHClientProtocol,
    timeout_seconds: int = _ADDON_UPDATE_TIMEOUT,
    interval: int = _POLL_INTERVAL_ADDON,
    _sleep: Optional[Callable] = None,
) -> bool:
    """Poll `ha apps info <slug>` until version matches and state=started or timeout."""
    sleep_fn = _sleep or asyncio.sleep
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            _, stdout, _ = await ssh_client.run(
                f"ha apps info {slug} --raw-json", check=False
            )
            data = json.loads(stdout)
            info = data.get("data", data)
            version = info.get("version", "")
            state = info.get("state", "")
            if version == target_version and state == "started":
                return True
        except Exception as exc:
            log.warning("poll_addon_version_error", slug=slug, error=str(exc))
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _poll_addon_update_via_rest(
    entity_id: str,
    ha_rest_client: HARestClientProtocol,
    timeout_seconds: int = _ADDON_UPDATE_TIMEOUT,
    interval: int = _POLL_INTERVAL_ADDON,
    _sleep: Optional[Callable] = None,
) -> bool:
    """Poll the HA REST update entity until state flips to 'off' (update applied)."""
    sleep_fn = _sleep or asyncio.sleep
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            entity = await ha_rest_client.get_state(entity_id)
            state = entity.get("state", "on")
            in_progress = bool(entity.get("attributes", {}).get("in_progress", False))
            if state == "off" and not in_progress:
                log.info("poll_addon_rest_success", entity_id=entity_id)
                return True
        except Exception as exc:
            log.warning("poll_addon_rest_error", entity_id=entity_id, error=str(exc))
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _send_post_update_card(
    update: UpdateStatus,
    notifier: "NotifierProtocol",
    success: bool,
    config_check_output: str,
    log_triage_summary: str,
    notification_id: Optional[str] = None,
    self_check: Optional["PueoSelfCheckResult"] = None,
) -> None:
    """Send an informational result card after an update attempt."""
    nid = notification_id or str(uuid.uuid4())
    outcome = "succeeded" if success else "timed out — check HA UI"
    subject = f"Update {outcome}: {update.component} → {update.latest_version}"
    body_lines = [
        f"Component: {update.component}",
        f"Version: {update.installed_version} → {update.latest_version}",
        f"Outcome: {outcome}",
    ]
    if config_check_output:
        body_lines.append(f"Config check: {config_check_output[:200]}")
    if log_triage_summary:
        body_lines.append(f"Log triage: {log_triage_summary}")
    if self_check is not None:
        sc_status = "OK" if self_check.all_commands_ok else "DEGRADED"
        body_lines.append(f"Pueo self-check: {sc_status}")
        if self_check.command_risks:
            body_lines.append(
                f"Command risks: {'; '.join(self_check.command_risks[:3])}"
            )
    body = "\n".join(body_lines)
    payload: dict = {
        "notification_id": nid,
        "type": "update_result",
        "component": update.component,
        "installed_version": update.installed_version,
        "latest_version": update.latest_version,
        "success": success,
        "config_check_output": config_check_output,
        "log_triage_summary": log_triage_summary,
    }
    if self_check is not None:
        payload["self_check"] = {
            "core_check_ok": self_check.core_check_ok,
            "core_info_ok": self_check.core_info_ok,
            "apps_list_ok": self_check.apps_list_ok,
            "netalertx_info_ok": self_check.netalertx_info_ok,
            "backup_smoke_ok": self_check.backup_smoke_ok,
            "command_risks": self_check.command_risks,
            "all_commands_ok": self_check.all_commands_ok,
        }
    log.info(
        "post_update_card_sent",
        component=update.component,
        version=update.latest_version,
        success=success,
        notification_id=nid,
    )
    await notifier.send(subject, body, payload)


async def execute_core_update(
    update: UpdateStatus,
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    gate: "AutonomyGate | FakeAutonomyGate",
    llm_client: Optional[LLMClientProtocol] = None,
    _poll: Optional[Callable] = None,
    disk_free_gb: Optional[float] = None,
    cache_dir: Optional[str] = None,
) -> bool:
    """Execute Core update: backup → ha core update → poll → post-check → result card."""
    from ha_agent_advanced import (
        execute_remote_backup,
        offload_backup_to_local,
        record_backup_slug,
    )

    log.info("core_update_start", version=update.latest_version)
    backup_slug = await execute_remote_backup(ssh_client=ssh_client)
    record_backup_slug(backup_slug)
    await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
    log.info("core_update_backup_complete", slug=backup_slug)

    _, _, update_stderr = await ssh_client.run(
        "ha core update --no-progress", check=False
    )
    if update_stderr:
        log.warning("core_update_stderr", stderr=update_stderr[:200])

    poll_fn = _poll or _poll_core_version
    success = await poll_fn(
        update.latest_version, ssh_client, timeout_seconds=_CORE_UPDATE_TIMEOUT
    )

    config_check_output = ""
    log_triage_summary = ""
    self_check: Optional[PueoSelfCheckResult] = None
    if success:
        try:
            _, cc_out, cc_err = await ssh_client.run("ha core check", check=False)
            config_check_output = (cc_out or cc_err or "").strip()
        except Exception as exc:
            log.warning("post_update_config_check_failed", error=str(exc))

        try:
            _, log_out, _ = await ssh_client.run(
                "ha core logs --lines 100", check=False
            )
            if log_out:
                from ha_log_monitor import analyze_log_line_with_ai

                lines = log_out.splitlines()
                evaluation, _ = await analyze_log_line_with_ai(lines, llm_client)
                log_triage_summary = evaluation.root_cause_summary
                log.info(
                    "post_update_log_triage",
                    actionable=evaluation.is_actionable,
                    confidence=evaluation.confidence_score,
                )
        except Exception as exc:
            log.warning("post_update_log_triage_failed", error=str(exc))

        try:
            self_check = await run_pueo_self_check(
                ssh_client,
                version=update.latest_version,
                cache_dir=cache_dir,
                disk_free_gb=disk_free_gb,
                llm_client=llm_client,
            )
        except Exception as exc:
            log.warning("post_update_self_check_failed", error=str(exc))

    await _send_post_update_card(
        update,
        notifier,
        success,
        config_check_output,
        log_triage_summary,
        self_check=self_check,
    )

    if not success:
        log.warning(
            "core_update_timed_out",
            version=update.latest_version,
            detail="Update may still be in progress — check HA UI",
        )
    return success


async def execute_os_update(
    update: UpdateStatus,
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    gate: "AutonomyGate | FakeAutonomyGate",
    ha_host: Optional[str] = None,
    ha_port: Optional[int] = None,
    _poll: Optional[Callable] = None,
) -> bool:
    """Execute OS update: backup → ha os update → TCP poll → result card."""
    from ha_agent_advanced import (
        execute_remote_backup,
        offload_backup_to_local,
        record_backup_slug,
    )

    log.info("os_update_start", version=update.latest_version)
    backup_slug = await execute_remote_backup(ssh_client=ssh_client)
    record_backup_slug(backup_slug)
    await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
    log.info("os_update_backup_complete", slug=backup_slug)

    await ssh_client.run("ha os update --no-progress", check=False)

    host = ha_host or HA_HOST
    port = ha_port or HA_API_PORT
    poll_fn = _poll or _poll_os_online
    success = await poll_fn(host, port, timeout_seconds=_OS_UPDATE_TIMEOUT)

    await _send_post_update_card(update, notifier, success, "", "")

    if not success:
        log.warning(
            "os_update_timed_out",
            version=update.latest_version,
            detail="HA did not come back online — check HA UI",
        )
    return success


async def execute_addon_update(
    update: UpdateStatus,
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    gate: "AutonomyGate | FakeAutonomyGate",
    _poll: Optional[Callable] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
) -> bool:
    """Execute add-on update: backup → update.install REST service → poll → result card."""
    from ha_agent_advanced import (
        execute_remote_backup,
        offload_backup_to_local,
        record_backup_slug,
    )

    log.info("addon_update_start", slug=update.component, version=update.latest_version)
    backup_slug = await execute_remote_backup(ssh_client=ssh_client)
    record_backup_slug(backup_slug)
    await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
    log.info("addon_update_backup_complete", slug=backup_slug)

    rest: HARestClientProtocol
    if ha_rest_client is not None:
        rest = ha_rest_client
    else:
        from utils.ha_rest_client import HARestClient

        rest = HARestClient(HA_HOST, HA_API_PORT, HA_API_TOKEN, timeout=30.0)

    try:
        await rest.call_service("update", "install", {"entity_id": update.entity_id})
        log.info(
            "addon_update_install_service_called",
            slug=update.component,
            entity_id=update.entity_id,
        )
    except httpx.TimeoutException:
        # HA holds the connection open while the update runs; a timeout here is expected
        # behaviour, not a failure. The update may already be in progress — proceed to
        # the poll, which will determine the actual outcome.
        log.warning(
            "addon_update_service_timeout",
            slug=update.component,
            entity_id=update.entity_id,
            detail="service call timed out; update may be in progress — proceeding to poll",
        )
    except Exception as exc:
        log.warning(
            "addon_update_service_failed",
            slug=update.component,
            error=str(exc)[:200] or repr(exc),
        )
        await _send_post_update_card(update, notifier, False, "", "")
        return False

    if _poll is not None:
        success = await _poll(
            update.component,
            update.latest_version,
            ssh_client,
            timeout_seconds=_ADDON_UPDATE_TIMEOUT,
        )
    else:
        success = await _poll_addon_update_via_rest(
            update.entity_id,
            rest,
            timeout_seconds=_ADDON_UPDATE_TIMEOUT,
        )

    await _send_post_update_card(update, notifier, success, "", "")

    if not success:
        log.warning(
            "addon_update_timed_out",
            slug=update.component,
            version=update.latest_version,
            detail="Add-on may still be updating — check HA UI",
        )
    return success


_UPDATE_PRIORITY: dict[str, int] = {"os": 0, "supervisor": 1, "core": 2}


def _update_priority(component: str) -> int:
    return _UPDATE_PRIORITY.get(component, 3)


def _pending_higher_priority_components(component: str, watch_dir: Path) -> list[str]:
    """Return component names of PENDING update cards with higher priority than ``component``."""
    this_priority = _update_priority(component)
    higher: list[str] = []
    for json_path in watch_dir.glob("*.json"):
        stem = json_path.stem
        if any(
            (watch_dir / f"{stem}.{s}").exists()
            for s in ("approved", "rejected", "deferred", "in_progress")
        ):
            continue
        try:
            other_payload = (
                __import__("json").loads(json_path.read_text()).get("payload", {})
            )
        except Exception:  # nosec B112
            continue
        from utils.card_types import CARD_TYPE_UPDATE

        if other_payload.get("card_type") != CARD_TYPE_UPDATE:
            continue
        other_component = other_payload.get("component", "")
        if _update_priority(other_component) < this_priority:
            higher.append(other_component)
    higher.sort(key=_update_priority)
    return higher


async def execute_ha_reboot(
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    ha_host: Optional[str] = None,
    ha_port: Optional[int] = None,
    _poll: Optional[Callable] = None,
    _api_poll: Optional[Callable] = None,
    _down_poll: Optional[Callable] = None,
) -> bool:
    """Reboot HA host: backup → reboot → wait for down → TCP poll → API ready → card."""
    from ha_agent_advanced import (
        execute_remote_backup,
        offload_backup_to_local,
        record_backup_slug,
    )

    log.info("ha_reboot_start")
    backup_slug = await execute_remote_backup(ssh_client=ssh_client)
    record_backup_slug(backup_slug)
    await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
    log.info("ha_reboot_backup_complete", slug=backup_slug)

    await ssh_client.run("ha host reboot", check=False)

    host = ha_host or HA_HOST
    port = ha_port or HA_API_PORT

    # Wait 10 s for ha-supervisor to begin shutdown, then confirm TCP drops.
    await asyncio.sleep(10)
    down_fn = _down_poll or _wait_for_ha_down
    await down_fn(host, port, timeout_seconds=60)

    # Wait for TCP to accept connections again.
    poll_fn = _poll or _poll_os_online
    success = await poll_fn(host, port, timeout_seconds=_OS_UPDATE_TIMEOUT)

    # Confirm the HTTP API layer is actually ready (TCP open ≠ API ready).
    if success:
        api_fn = _api_poll or _poll_ha_api_ready
        success = await api_fn(host, port, HA_API_TOKEN, timeout_seconds=120)
        if not success:
            log.warning(
                "ha_reboot_api_not_ready", detail="TCP up but HTTP API timed out"
            )

    reboot_update = UpdateStatus(
        component="reboot",
        entity_id="",
        installed_version="",
        latest_version="rebooted",
        update_available=False,
        release_url=None,
        release_summary=None,
        in_progress=False,
    )
    await _send_post_update_card(reboot_update, notifier, success, "", "")

    if not success:
        log.warning("ha_reboot_timed_out", detail="HA did not come back online")
    return success


async def execute_update(
    update: UpdateStatus,
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    gate: "AutonomyGate | FakeAutonomyGate",
    llm_client: Optional[LLMClientProtocol] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
) -> bool:
    """Dispatch update execution by component type."""
    from ha_agent_advanced import is_reboot_required_active

    if is_reboot_required_active():
        log.warning("update_blocked_reboot_required", component=update.component)
        await notifier.send(
            subject=f"Update blocked: {update.component}",
            body=(
                "HA requires a reboot before this update can proceed. "
                "Approve the reboot repair card first."
            ),
            payload={},
        )
        return False

    if update.component == "core":
        return await execute_core_update(update, ssh_client, notifier, gate, llm_client)
    if update.component == "os":
        return await execute_os_update(update, ssh_client, notifier, gate)
    return await execute_addon_update(
        update, ssh_client, notifier, gate, ha_rest_client=ha_rest_client
    )
