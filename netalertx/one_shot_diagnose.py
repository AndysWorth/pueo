"""NetAlertX one-shot diagnosis — item 27.

Proactively inspects the current state of NetAlertX and the HA integration,
synthesises an AI diagnosis, and optionally triggers healing via NetAlertXHealer.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Optional

from config import (
    AUTONOMY_LEVEL,
    HA_HOST,
    HA_USER,
    NETALERTX_ADDON_SLUG,
    NETALERTX_API_PORT,
    NETALERTX_API_TOKEN,
    NETALERTX_HOST,
    NETALERTX_SSH_HOST,
    NETALERTX_SSH_KEY_PATH,
    NETALERTX_SSH_USER,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    SSH_KEY_PATH,
)
from netalertx.config_validator import ConfigIssue, validate_app_conf
from netalertx.diagnosis import diagnose_health_report
from netalertx.health import NetAlertXHealthMonitor
from netalertx.log_monitor import CRITICAL_LOG_PATTERN, analyze_log_line_with_ai
from utils.autonomy import RiskLevel
from utils.logging import get_logger

if TYPE_CHECKING:
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.diagnosis import NetAlertXDiagnostic
    from netalertx.health import HealthReport
    from netalertx.healer import NetAlertXHealer
    from netalertx.log_monitor import LogEvaluation
    from interfaces import LLMClientProtocol, SSHClientProtocol
    from utils.autonomy import AutonomyGate
    from utils.notify import NotifierProtocol

log = get_logger("netalertx.one_shot_diagnose")

_LOG_SNAPSHOT_LINES = 100
_ADDON_STATE_RE = re.compile(r"state:\s*(\S+)", re.IGNORECASE)


async def _check_mosquitto_running(ha_ssh_client: "SSHClientProtocol") -> bool:
    """Return True if the core_mosquitto add-on is in running state on HA.

    Uses the same state string normalization as the installer: HA supervisor
    reports 'started' for a running add-on.
    """
    _, stdout, _ = await ha_ssh_client.run("ha apps info core_mosquitto")
    m = _ADDON_STATE_RE.search(stdout)
    if not m:
        return False
    raw = m.group(1).lower()
    state = "running" if raw == "started" else raw
    return state == "running"


async def _fetch_log_snapshot(
    ha_ssh_client: "SSHClientProtocol", slug: str
) -> list[str]:
    """Return the last 100 lines of NetAlertX logs via `ha apps logs`.

    Returns an empty list if the slug is unknown or the command produces no output.
    On HA OS, `docker` is not in PATH for SSH sessions; `ha apps logs` is the
    correct way to read add-on log output.
    """
    if not slug:
        return []
    _, stdout, _ = await ha_ssh_client.run(f"ha apps logs {slug}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    return lines[-_LOG_SNAPSHOT_LINES:]


async def _fetch_app_conf(ha_ssh_client: "SSHClientProtocol", slug: str) -> str | None:
    """Read app.conf from the HA add-on config directory via SFTP.

    On HA OS, add-on config files live at /addon_configs/<slug>/config/app.conf
    on the host filesystem — directly readable via SFTP without docker exec.
    Returns None if the slug is unknown or the file does not exist.
    """
    if not slug:
        return None
    conf_path = f"/addon_configs/{slug}/config/app.conf"
    try:
        return await ha_ssh_client.read_file(conf_path)
    except (FileNotFoundError, OSError):
        return None


def _print_summary(
    report: "HealthReport",
    log_evaluation: "Optional[LogEvaluation]",
    config_issues: list,
    diagnostic: "Optional[NetAlertXDiagnostic]",
) -> None:
    """Emit structured log lines rendered by the plain-text console formatter."""
    log.info(
        "netalertx_diagnose_health",
        scan_age_minutes=report.last_scan_age_minutes,
        device_counts=report.device_counts,
        mqtt_active=report.mqtt_active,
        netalertx_version=report.netalertx_version,
        anomalies=report.anomalies,
    )
    if config_issues:
        log.info(
            "netalertx_diagnose_config_issues",
            count=len(config_issues),
            issues=[f"[{i.severity}] {i.field}: {i.message}" for i in config_issues],
        )
    else:
        log.info("netalertx_diagnose_config_ok")
    if log_evaluation is not None:
        log.info(
            "netalertx_diagnose_log_triage",
            actionable=log_evaluation.is_actionable,
            cause=log_evaluation.root_cause_summary,
            confidence=log_evaluation.confidence_score,
        )
    if diagnostic is None:
        log.info("netalertx_diagnose_result", status="healthy", issue="none")
    else:
        log.info(
            "netalertx_diagnose_result",
            status="issues_found",
            severity=diagnostic.severity,
            category=diagnostic.category,
            issue=diagnostic.issue,
            recommended_fix=diagnostic.recommended_fix,
        )


async def run_diagnose(
    ssh_client: "SSHClientProtocol | None" = None,
    ha_ssh_client: "SSHClientProtocol | None" = None,
    api_client: "NetAlertXAPIClient | None" = None,
    llm_client: "LLMClientProtocol | None" = None,
    gate: "AutonomyGate | None" = None,
    notifier: "NotifierProtocol | None" = None,
    healer: "NetAlertXHealer | None" = None,
    addon_slug: str | None = None,
) -> None:
    """One-shot NetAlertX diagnosis and optional healing.

    Entry point for ``--mode netalertx-diagnose``.
    """
    from netalertx.api_client import NetAlertXAPIClient
    from utils.autonomy import AutonomyGate
    from utils.notify import get_notifier
    from utils.ssh_client import AsyncSSHClient

    _ssh = ssh_client or AsyncSSHClient(
        NETALERTX_SSH_HOST, NETALERTX_SSH_USER, NETALERTX_SSH_KEY_PATH
    )
    _ha_ssh = ha_ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    _api = api_client or NetAlertXAPIClient(
        base_url=f"http://{NETALERTX_HOST}:{NETALERTX_API_PORT}",
        api_token=NETALERTX_API_TOKEN,
    )
    _gate = gate or AutonomyGate(level=AUTONOMY_LEVEL)
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
    _slug = addon_slug if addon_slug is not None else NETALERTX_ADDON_SLUG

    # 1. Connectivity check — exit early if NetAlertX is unreachable
    try:
        await _api.get_about()
    except Exception as exc:
        log.error("netalertx_api_unreachable", error=str(exc))
        return

    # 2. Health report (no MQTT subscriber needed for one-shot)
    monitor = NetAlertXHealthMonitor(api_client=_api)
    report = await monitor.poll_once(asyncio.Queue())

    # 2b. Active MQTT infrastructure check — poll_once() can't detect MQTT
    # activity without a subscriber, so we SSH to check Mosquitto directly.
    mosquitto_running = await _check_mosquitto_running(_ha_ssh)
    log.info("netalertx_diagnose_mqtt_infra", mosquitto_running=mosquitto_running)

    # 3. Log snapshot triage — uses `ha apps logs` via HA SSH; on HA OS, docker
    # is not in PATH for SSH sessions so docker exec cannot be used.
    log_lines = await _fetch_log_snapshot(_ha_ssh, _slug)
    log_evaluation = None
    if any(CRITICAL_LOG_PATTERN.search(line) for line in log_lines):
        log_evaluation, _ = await analyze_log_line_with_ai(
            log_lines, llm_client=llm_client
        )

    # 4. Config validation — read via SFTP from the host addon config directory.
    # On HA OS, add-on config files are at /addon_configs/<slug>/config/app.conf.
    if not _slug:
        config_issues: list[ConfigIssue] = [
            ConfigIssue(
                field="netalertx.addon_slug",
                message=(
                    "netalertx.addon_slug is not set in config.yaml — "
                    "cannot read app.conf or fetch logs. "
                    "Run netalertx-setup or set addon_slug manually."
                ),
                severity="HIGH",
            )
        ]
    else:
        conf_text = await _fetch_app_conf(_ha_ssh, _slug)
        if conf_text is None:
            conf_host_path = f"/addon_configs/{_slug}/config/app.conf"
            config_issues = [
                ConfigIssue(
                    field="app.conf",
                    message=(
                        f"NetAlertX app.conf not found at {conf_host_path} — "
                        "add-on may not have written its config yet; "
                        "try re-running netalertx-setup."
                    ),
                    severity="HIGH",
                )
            ]
        else:
            config_issues = validate_app_conf(conf_text)

    if not mosquitto_running:
        config_issues.append(
            ConfigIssue(
                field="core_mosquitto",
                message=(
                    "Mosquitto MQTT broker (core_mosquitto) is not running on HA — "
                    "MQTT publishing to HA will fail. "
                    "Run 'ha apps start core_mosquitto' or re-run netalertx-setup."
                ),
                severity="HIGH",
            )
        )

    # 5. AI synthesis
    diagnostic, _llm_trace = await diagnose_health_report(
        report, config_issues, llm_client
    )

    # 6. Print summary
    _print_summary(report, log_evaluation, config_issues, diagnostic)

    # 7. Optional healing — only when the gate can auto-proceed without HITL.
    # The healer's minimum risk is MEDIUM; at autonomy levels 1–3, require_approval()
    # blocks indefinitely waiting for dashboard input, which is not appropriate for
    # a one-shot command. Only autonomy level 4 (AUTONOMOUS) auto-executes HIGH risk.
    if diagnostic is not None:
        if _gate.should_auto_execute(RiskLevel.HIGH):
            _healer = healer
            if _healer is None:
                from netalertx.healer import NetAlertXHealer

                _healer = NetAlertXHealer(
                    gate=_gate,
                    ssh_client=_ssh,
                    ha_ssh_client=_ha_ssh,
                    api_client=_api,
                    notifier=_notifier,
                )
            await _healer.heal(diagnostic)
        else:
            log.info(
                "netalertx_diagnose_heal_skipped",
                reason="autonomy_level_requires_hitl",
                hint="Set autonomy_level=4 in config.yaml to auto-heal, "
                "or use the dashboard to approve the pending action.",
            )
