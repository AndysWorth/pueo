"""NetAlertX one-shot diagnosis — item 27.

Proactively inspects the current state of NetAlertX and the HA integration,
synthesises an AI diagnosis, and optionally triggers healing via NetAlertXHealer.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from config import (
    AUTONOMY_LEVEL,
    HA_HOST,
    HA_USER,
    NETALERTX_API_PORT,
    NETALERTX_API_TOKEN,
    NETALERTX_HOST,
    NETALERTX_LOG_CONTAINER_NAME,
    NETALERTX_SSH_HOST,
    NETALERTX_SSH_KEY_PATH,
    NETALERTX_SSH_USER,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    SSH_KEY_PATH,
)
from netalertx.config_validator import validate_app_conf
from netalertx.diagnosis import diagnose_health_report
from netalertx.health import NetAlertXHealthMonitor
from netalertx.log_monitor import CRITICAL_LOG_PATTERN, analyze_log_line_with_ai
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

_LOG_PATH = "/data/app.log"
_CONF_PATH = "/data/app.conf"
_LOG_SNAPSHOT_LINES = 100


async def _fetch_log_snapshot(
    ssh_client: "SSHClientProtocol", container: str
) -> list[str]:
    """Return the last 100 lines of the NetAlertX app log via docker exec.

    Returns an empty list if the container is unreachable or produces no output.
    """
    _, stdout, _ = await ssh_client.run(
        f"docker exec {container} tail -n {_LOG_SNAPSHOT_LINES} {_LOG_PATH}"
    )
    return [line for line in stdout.splitlines() if line.strip()]


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

    # 1. Connectivity check — exit early if NetAlertX is unreachable
    try:
        await _api.get_about()
    except Exception as exc:
        log.error("netalertx_api_unreachable", error=str(exc))
        return

    # 2. Health report (no MQTT subscriber needed for one-shot)
    monitor = NetAlertXHealthMonitor(api_client=_api)
    report = await monitor.poll_once(asyncio.Queue())

    # 3. Log snapshot triage
    log_lines = await _fetch_log_snapshot(_ssh, NETALERTX_LOG_CONTAINER_NAME)
    log_evaluation = None
    if any(CRITICAL_LOG_PATTERN.search(line) for line in log_lines):
        log_evaluation, _ = await analyze_log_line_with_ai(
            log_lines, llm_client=llm_client
        )

    # 4. Config validation
    try:
        conf_text = await _ssh.read_file(_CONF_PATH)
        config_issues = validate_app_conf(conf_text)
    except (FileNotFoundError, OSError):
        config_issues = []

    # 5. AI synthesis
    diagnostic, _llm_trace = await diagnose_health_report(
        report, config_issues, llm_client
    )

    # 6. Print summary
    _print_summary(report, log_evaluation, config_issues, diagnostic)

    # 7. Optional healing
    if diagnostic is not None:
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
