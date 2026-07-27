#!/usr/bin/env python3
"""HA Update Manager — detects available updates and surfaces them as HITL cards."""

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import httpx
from pydantic import BaseModel

from config import (
    HA_API_PORT,
    HA_API_TOKEN,
    HA_HOST,
    HA_UPDATE_RELEASE_NOTES_CACHE_DIR,
    MAX_PROMPT_TOKENS,
    OLLAMA_MODEL,
)
from interfaces import HARestClientProtocol, LLMClientProtocol, SSHClientProtocol
from utils.autonomy import RiskLevel
from utils.context import truncate_to_budget
from utils.ha_rest_client import HARestClient, UpdateStatus, get_update_status
from utils.logging import get_logger

if TYPE_CHECKING:
    from utils.autonomy import AutonomyGate, FakeAutonomyGate
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
    """Return cached release notes; fetch from GitHub API if not yet cached."""
    cache_path = Path(cache_dir) / f"{version}.txt"
    if cache_path.exists():
        return cache_path.read_text()
    fetcher = _fetcher or _fetch_github_release_notes
    notes = await fetcher(version)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(notes)
    return notes


async def analyze_breaking_changes(
    update_status: UpdateStatus,
    config_yaml_content: str,
    release_notes: str,
    llm_client: Optional[LLMClientProtocol] = None,
) -> UpdateReadinessReport:
    """LLM advisory analysis of release notes against the current installation."""
    from utils.ollama_client import OllamaClient

    client: LLMClientProtocol = llm_client or OllamaClient()  # pragma: no cover

    command_catalog = "\n".join(f"  - {cmd}" for cmd in PUEO_SSH_COMMANDS)
    config_budget = MAX_PROMPT_TOKENS // 2
    notes_budget = MAX_PROMPT_TOKENS - 400 - config_budget  # 400 for system + catalog

    config_content = truncate_to_budget(config_yaml_content, config_budget)
    notes_content = truncate_to_budget(release_notes, notes_budget)

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
            "content": (
                f"Target version: {update_status.latest_version}\n"
                f"Installed version: {update_status.installed_version}\n\n"
                f"=== Current configuration.yaml ===\n{config_content}\n\n"
                f"=== Release notes ===\n{notes_content}\n\n"
                f"=== Pueo SSH command catalog ===\n{command_catalog}\n\n"
                "List breaking changes, affected config keys, and any Pueo commands "
                "that appear in breaking changes or migration notes."
            ),
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
) -> bool:
    """Send a per-component HITL update approval card and wait for a decision.

    Returns True if the update should proceed, False if rejected or deferred.
    Risk is always CRITICAL for core/os, HIGH for supervisor, MEDIUM for add-ons.
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
    body = "\n".join(body_parts)

    payload: dict = {
        "notification_id": nid,
        "component": update.component,
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
) -> list[UpdateStatus]:
    """One-shot: print update status table and advisory breaking-change analysis."""
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

    # Breaking-change analysis for any Core update.
    core_updates = [u for u in available if u.component == "core"]
    for core_update in core_updates:
        config_yaml_content = ""
        if ssh_client:
            try:
                from config import CONFIG_REMOTE_PATH

                config_yaml_content = await ssh_client.read_file(CONFIG_REMOTE_PATH)
            except Exception as exc:
                log.warning("config_fetch_skipped", error=str(exc))

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
                core_update, config_yaml_content, release_notes, llm_client
            )
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

    return updates
