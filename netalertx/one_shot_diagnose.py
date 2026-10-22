"""NetAlertX one-shot diagnosis — item 27.

Proactively inspects the current state of NetAlertX and the HA integration,
synthesises an AI diagnosis, and optionally triggers healing via NetAlertXHealer.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Optional

from config import (
    AUTONOMY_LEVEL,
    HA_HOST,
    HA_USER,
    NETALERTX_ADDON_REPOSITORY_URL,
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
from netalertx.config_validator import (
    ConfigIssue,
    validate_app_conf,
    validate_scan_subnets,
)
from netalertx.diagnosis import diagnose_health_report
from netalertx.health import NetAlertXHealthMonitor
from netalertx.installer import _parse_slug_from_store, _write_addon_slug_to_config
from netalertx.log_monitor import CRITICAL_LOG_PATTERN, analyze_log_line_with_ai
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.diagnosis import NetAlertXDiagnostic
    from netalertx.health import HealthReport
    from netalertx.healer import NetAlertXHealer
    from netalertx.log_monitor import LogEvaluation
    from interfaces import LLMClientProtocol, SSHClientProtocol
    from utils.agent.autonomy import AutonomyGate
    from utils.hitl.notify import NotifierProtocol
    from utils.knowledge.knowledge_store import ChromaKnowledgeStore

log = get_logger("netalertx.one_shot_diagnose")

_LOG_SNAPSHOT_LINES = 100
_ADDON_STATE_RE = re.compile(r"state:\s*(\S+)", re.IGNORECASE)


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


async def _fetch_app_conf(
    ha_ssh_client: "SSHClientProtocol", slug: str, root: str = ""
) -> str | None:
    """Read app.conf from the HA app-config directory via SFTP.

    On HA OS, add-on config files live at /app_configs/<slug>/config/app.conf
    (renamed from /addon_configs in HA 2026.x). Pass a resolved root from
    resolve_app_config_root() for accuracy; falls back to /app_configs.
    Returns None if the slug is unknown or the file does not exist.
    """
    if not slug:
        return None
    effective_root = root or "/app_configs"
    conf_path = f"{effective_root}/{slug}/config/app.conf"
    try:
        return await ha_ssh_client.read_file(conf_path)
    except (FileNotFoundError, OSError):
        return None


async def _check_sidebar_status(
    ha_ssh_client: "SSHClientProtocol",
    slug: str,
    config_issues: list[ConfigIssue],
) -> None:
    """Append a ConfigIssue when ingress_panel is disabled for the add-on."""
    if not slug:
        return
    _, stdout, _ = await ha_ssh_client.run(f"ha apps info {slug}")
    m = re.search(r"ingress_panel:\s*(\S+)", stdout, re.IGNORECASE)
    if m and m.group(1).lower() in ("false", "no", "0"):
        config_issues.append(
            ConfigIssue(
                field="ingress_panel",
                message=(
                    f"NetAlertX add-on ({slug}) is not shown in the HA sidebar "
                    "(ingress_panel=false). Re-run netalertx-setup or set "
                    "netalertx.sidebar: true in config.yaml and re-run."
                ),
                severity="LOW",
            )
        )


async def _check_and_refresh_token(
    ha_ssh_client: "SSHClientProtocol",
    slug: str,
    app_config_root: str,
    config_issues: list[ConfigIssue],
) -> None:
    """Try to auto-read the API token from app.db when it is not set in config.

    Persists the token to config.yaml on success.  Appends a MEDIUM ConfigIssue
    when the token is still missing after the attempt.
    """
    import config as _cfg_dyn

    if getattr(_cfg_dyn, "NETALERTX_API_TOKEN", ""):
        return  # already configured

    if not slug:
        config_issues.append(
            ConfigIssue(
                field="netalertx.api_token",
                message=(
                    "netalertx.api_token is not set and addon_slug is unknown — "
                    "cannot auto-read token. Set both in config.yaml."
                ),
                severity="MEDIUM",
            )
        )
        return

    from netalertx.token import persist_api_token, read_api_token

    token = await read_api_token(ha_ssh_client, slug, app_config_root)
    if token:
        if persist_api_token(token):
            log.info(
                "netalertx_diagnose_token_auto_persisted",
                note="token read from app.db and written to config.yaml",
            )
        else:
            log.warning(
                "netalertx_diagnose_token_persist_failed",
                note="token found but config.yaml update failed; set netalertx.api_token manually",
            )
    else:
        config_issues.append(
            ConfigIssue(
                field="netalertx.api_token",
                message=(
                    "netalertx.api_token is not set and could not be auto-read from "
                    "app.db. Generate a token at: "
                    "NetAlertX UI → Settings → Main Settings → API Key, "
                    "then set netalertx.api_token in config.yaml."
                ),
                severity="MEDIUM",
            )
        )


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
    knowledge_store: "ChromaKnowledgeStore | None" = None,
) -> None:
    """One-shot NetAlertX diagnosis and optional healing.

    Entry point for ``--mode netalertx-diagnose`` and the dashboard /netalertx/diagnose
    route.  Pass ``knowledge_store`` when calling from the dashboard so the diagnosis
    loop can query the shared ChromaDB instance instead of opening a second one.
    """
    from netalertx.api_client import NetAlertXAPIClient
    from utils.agent.autonomy import AutonomyGate
    from utils.core.timeline import write_timeline_event
    from utils.hitl.notify import get_notifier
    from utils.ha.ssh_client import AsyncSSHClient

    await asyncio.to_thread(
        write_timeline_event,
        "INFO",
        "netalertx_diagnose",
        "NetAlertX one-shot diagnosis started",
    )

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

    # 2. Health report — REST API poll.
    monitor = NetAlertXHealthMonitor(api_client=_api)
    report = await monitor.poll_once()

    # 3. Log snapshot triage — uses `ha apps logs` via HA SSH; on HA OS, docker
    # is not in PATH for SSH sessions so docker exec cannot be used.
    log_lines = await _fetch_log_snapshot(_ha_ssh, _slug)
    log_evaluation = None
    if any(CRITICAL_LOG_PATTERN.search(line) for line in log_lines):
        log_evaluation, _ = await analyze_log_line_with_ai(
            log_lines, llm_client=llm_client
        )

    # 4. Config validation — read via SFTP from the host app config directory.
    # HA renamed /addon_configs → /app_configs in HA 2026.x (Add-ons → Apps).
    from utils.ha.app_configs import resolve_app_config_root

    _app_config_root = await resolve_app_config_root(_ha_ssh)
    if not _slug:
        # Attempt slug auto-discovery before flagging as an error.
        # This recovers existing installs where setup completed without writing the slug.
        _, addons_out, _ = await _ha_ssh.run("ha store addons")
        discovered = _parse_slug_from_store(addons_out, NETALERTX_ADDON_REPOSITORY_URL)
        if discovered:
            _slug = discovered
            if _write_addon_slug_to_config(discovered):
                log.info("netalertx_diagnose_slug_discovered", slug=discovered)

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
        conf_text = await _fetch_app_conf(_ha_ssh, _slug, root=_app_config_root)
        if conf_text is None:
            _root = _app_config_root or "/app_configs"
            conf_host_path = f"{_root}/{_slug}/config/app.conf"
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
            config_issues.extend(validate_scan_subnets(conf_text))

    # 4b. Sidebar status
    await _check_sidebar_status(_ha_ssh, _slug, config_issues)

    # 4c. API token — try auto-read from app.db if token is empty
    await _check_and_refresh_token(_ha_ssh, _slug, _app_config_root, config_issues)

    # 5. AI synthesis — pass _ha_ssh to enable adaptive AgentLoop evidence gathering
    diagnostic, _llm_trace = await diagnose_health_report(
        report,
        config_issues,
        llm_client,
        ssh_client=_ha_ssh,
        knowledge_store=knowledge_store,
    )

    # 6. Print summary
    _print_summary(report, log_evaluation, config_issues, diagnostic)

    _result_msg = (
        f"NetAlertX diagnosis complete: {diagnostic.severity} — {diagnostic.issue[:80]}"
        if diagnostic is not None
        else "NetAlertX diagnosis complete: no issues found"
    )
    await asyncio.to_thread(
        write_timeline_event,
        "INFO" if diagnostic is None else "WARN",
        "netalertx_diagnose",
        _result_msg,
    )

    # 7. Heal — the healer + autonomy gate handle auto-execute vs. approval-required blocking.
    # At level 4, healer runs immediately. At levels 2–3, healer calls
    # require_approval(), sends a dashboard notification, and blocks until the
    # user approves or rejects. At level 1 (report only), require_approval
    # returns False without notifying and no action is taken.
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
        # Use the AgentLoop-based repair path (item 46).
        from utils.llm.llm_factory import make_llm_client

        _heal_llm = make_llm_client()
        _healer_snap = _healer

        async def _heal_coro(
            _h: "Any" = _healer_snap,
            _lc: "Any" = _heal_llm,
        ) -> None:
            await _h.heal_with_loop(diagnostic, llm_client=_lc)

        from utils.agent.work_queue import (
            PRIORITY_HIGH,
            WorkItem,
            get_work_queue_or_none,
        )

        _wq = get_work_queue_or_none()
        if _wq is not None:
            await _wq.submit(
                WorkItem(
                    priority=PRIORITY_HIGH,
                    activity_type="netalertx_repair",
                    description=f"NetAlertX repair: {diagnostic.issue[:80]}",
                    dedup_key="netalertx_repair",
                    suppress_while_running=frozenset(),
                    coro_factory=_heal_coro,
                )
            )
        else:
            await _heal_coro()
