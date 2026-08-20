"""Pueo web dashboard — queue approval/rejection of pending repair actions."""

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from config import (
    AGENT_MAX_WALL_SECONDS,
    DASHBOARD_PORT,
    DB_PATH,
    FEDERATED_CASES_REPO,
    NOTIFY_WATCH_DIR,
)
from utils.core.logging import get_logger
from utils.card_types import (
    CARD_TYPE_CLOUD_ESCALATION,
    CARD_TYPE_CODE_PROPOSAL,
    CARD_TYPE_DASHBOARD_ENTITY,
    CARD_TYPE_DISK_RECOVERY,
    CARD_TYPE_HA_REPAIR,
    CARD_TYPE_NETALERTX_HEAL,
    CARD_TYPE_NETALERTX_MIGRATE,
    CARD_TYPE_NETALERTX_SWITCH,
    CARD_TYPE_OPEN_PR,
    CARD_TYPE_REPAIR,
    CARD_TYPE_RESOURCE_ACTION,
    CARD_TYPE_UPDATE,
)

log = get_logger("dashboard")

app = FastAPI(title="Pueo Dashboard")
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["epoch_to_iso"] = lambda ts: (
    datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
)


class ConfigUpdateRequest(BaseModel):
    key: str
    value: str


_EDITABLE_PARAMS: dict[str, dict] = {
    # ── Autonomy ──────────────────────────────────────────────────────────────
    "autonomy_level": {
        "yaml_section": "agent",
        "yaml_key": "autonomy_level",
        "config_attr": "AUTONOMY_LEVEL",
        "val_type": "int",
        "description": "1 = report only · 2 = suggest + approve all · 3 = auto LOW, approve rest · 4 = auto LOW/MED/HIGH, approve CRITICAL",
        "group": "Autonomy",
        "restart_required": False,
        "min_val": 1,
        "max_val": 4,
    },
    "self_healing_enabled": {
        "yaml_section": "agent",
        "yaml_key": "self_healing_enabled",
        "config_attr": "SELF_HEALING_ENABLED",
        "val_type": "bool",
        "description": "Master switch — disabling stops all autonomous repair actions",
        "group": "Autonomy",
        "restart_required": False,
    },
    # ── Monitoring intervals ──────────────────────────────────────────────────
    "resource_poll_interval_seconds": {
        "yaml_section": "agent",
        "yaml_key": "resource_poll_interval_seconds",
        "config_attr": "RESOURCE_POLL_INTERVAL_SECONDS",
        "val_type": "float",
        "description": "How often to check HA disk and memory (seconds; 0 = disabled)",
        "group": "Monitoring intervals",
        "restart_required": False,
        "min_val": 0,
    },
    "update_check_interval_hours": {
        "yaml_section": "agent",
        "yaml_key": "update_check_interval_hours",
        "config_attr": "HA_UPDATE_CHECK_INTERVAL_HOURS",
        "val_type": "float",
        "description": "How often to check for HA updates (hours; 0 = disabled)",
        "group": "Monitoring intervals",
        "restart_required": False,
        "min_val": 0,
    },
    "notification_poll_interval_minutes": {
        "yaml_section": "agent",
        "yaml_key": "notification_poll_interval_minutes",
        "config_attr": "HA_NOTIFICATION_POLL_INTERVAL_MINUTES",
        "val_type": "float",
        "description": "How often to poll HA persistent notifications (minutes; 0 = disabled)",
        "group": "Monitoring intervals",
        "restart_required": False,
        "min_val": 0,
    },
    # ── Thresholds ────────────────────────────────────────────────────────────
    "log_confidence_threshold": {
        "yaml_section": "agent",
        "yaml_key": "log_confidence_threshold",
        "config_attr": "CONFIDENCE_THRESHOLD",
        "val_type": "float",
        "description": "Minimum LLM confidence score to treat a log line as actionable (0.0–1.0)",
        "group": "Thresholds",
        "restart_required": False,
        "min_val": 0.0,
        "max_val": 1.0,
    },
    "ha_disk_warn_gb": {
        "yaml_section": "agent",
        "yaml_key": "ha_disk_warn_gb",
        "config_attr": "HA_DISK_WARN_GB",
        "val_type": "float",
        "description": "HA disk free threshold for WARN alert (GB)",
        "group": "Thresholds",
        "restart_required": False,
        "min_val": 0,
    },
    "ha_disk_critical_gb": {
        "yaml_section": "agent",
        "yaml_key": "ha_disk_critical_gb",
        "config_attr": "HA_DISK_CRITICAL_GB",
        "val_type": "float",
        "description": "HA disk free threshold for CRITICAL alert — backups blocked below this (GB)",
        "group": "Thresholds",
        "restart_required": False,
        "min_val": 0,
    },
    "max_repairs_per_hour": {
        "yaml_section": "agent",
        "yaml_key": "max_repairs_per_hour",
        "config_attr": "MAX_REPAIRS_PER_HOUR",
        "val_type": "int",
        "description": "Maximum repair actions allowed per rolling hour",
        "group": "Thresholds",
        "restart_required": False,
        "min_val": 1,
    },
    # ── Dashboard ─────────────────────────────────────────────────────────────
    "timeline_page_size": {
        "yaml_section": "agent",
        "yaml_key": "timeline_page_size",
        "config_attr": "TIMELINE_PAGE_SIZE",
        "val_type": "int",
        "description": "Number of events shown per page on the Timeline tab",
        "group": "Dashboard",
        "restart_required": False,
        "min_val": 1,
    },
    # ── Notifications ─────────────────────────────────────────────────────────
    "notifier": {
        "yaml_section": "agent",
        "yaml_key": "notifier",
        "config_attr": "NOTIFIER",
        "val_type": "str",
        "description": "Approval notification delivery method",
        "group": "Notifications",
        "restart_required": False,
        "options": ["file", "ntfy", "webhook"],
    },
    "notify_url": {
        "yaml_section": "agent",
        "yaml_key": "notify_url",
        "config_attr": "NOTIFY_URL",
        "val_type": "str",
        "description": "ntfy topic URL or webhook endpoint (leave blank for file notifier)",
        "group": "Notifications",
        "restart_required": False,
    },
    # ── Model Selection ───────────────────────────────────────────────────────
    "ollama_model": {
        "yaml_section": "ollama",
        "yaml_key": "model",
        "config_attr": "OLLAMA_MODEL",
        "val_type": "str",
        "description": "Active Ollama model name for local inference",
        "group": "Model Selection",
        "restart_required": False,
    },
    "ollama_model_auto": {
        "yaml_section": "ollama",
        "yaml_key": "model_auto",
        "config_attr": "OLLAMA_MODEL_AUTO",
        "val_type": "bool",
        "description": "Auto-select the best model for this hardware at startup",
        "group": "Model Selection",
        "restart_required": False,
    },
    # ── LLM Provider (restart required) ──────────────────────────────────────
    "llm_provider": {
        "yaml_section": "llm",
        "yaml_key": "provider",
        "config_attr": "LLM_PROVIDER",
        "val_type": "str",
        "description": "local = Ollama only (no WAN) · cloud = Anthropic API · both = Ollama for autonomous cycles + Claude available for approved escalation",
        "group": "LLM Provider",
        "restart_required": True,
        "options": ["local", "cloud", "both"],
    },
    "cloud_model": {
        "yaml_section": "cloud",
        "yaml_key": "model",
        "config_attr": "CLOUD_MODEL",
        "val_type": "str",
        "description": "Claude model used in cloud/both modes",
        "group": "LLM Provider",
        "restart_required": True,
    },
    "cloud_max_cost_per_incident_usd": {
        "yaml_section": "cloud",
        "yaml_key": "max_cost_per_incident_usd",
        "config_attr": "CLOUD_MAX_COST_PER_INCIDENT_USD",
        "val_type": "float",
        "description": "Hard per-incident spend cap in USD (cloud/both modes only)",
        "group": "LLM Provider",
        "restart_required": False,
        "min_val": 0.0,
        "max_val": 50.0,
    },
    "cloud_max_daily_spend_usd": {
        "yaml_section": "cloud",
        "yaml_key": "max_daily_spend_usd",
        "config_attr": "CLOUD_MAX_DAILY_SPEND_USD",
        "val_type": "float",
        "description": "Rolling 24-hour spend cap in USD (cloud/both modes only)",
        "group": "LLM Provider",
        "restart_required": False,
        "min_val": 0.0,
        "max_val": 500.0,
    },
    # ── HA Connection (restart required) ──────────────────────────────────────
    "ha_host": {
        "yaml_section": "home_assistant",
        "yaml_key": "host",
        "config_attr": "HA_HOST",
        "val_type": "str",
        "description": "Home Assistant hostname or IP address",
        "group": "HA Connection",
        "restart_required": True,
    },
    "ha_user": {
        "yaml_section": "home_assistant",
        "yaml_key": "user",
        "config_attr": "HA_USER",
        "val_type": "str",
        "description": "SSH user on the HA host",
        "group": "HA Connection",
        "restart_required": True,
    },
    "ha_api_port": {
        "yaml_section": "home_assistant",
        "yaml_key": "api_port",
        "config_attr": "HA_API_PORT",
        "val_type": "int",
        "description": "HA REST API port",
        "group": "HA Connection",
        "restart_required": True,
        "min_val": 1,
        "max_val": 65535,
    },
}


class HITLRequest(BaseModel):
    notification_id: str
    subject: str
    body: str
    payload: dict[str, Any]
    sent_at: int
    status: str
    elapsed_seconds: int
    in_progress: bool = False

    @field_validator("status")
    @classmethod
    def _status_must_be_known(cls, v: str) -> str:
        if v not in {"PENDING", "APPROVED", "REJECTED", "DEFERRED"}:
            raise ValueError(f"unknown status: {v}")
        return v


def _status(nid: str, watch_dir: Path) -> str:
    if (watch_dir / f"{nid}.approved").exists():
        return "APPROVED"
    if (watch_dir / f"{nid}.deferred").exists():
        return "DEFERRED"
    if (watch_dir / f"{nid}.rejected").exists():
        return "REJECTED"
    return "PENDING"


def _load_requests(watch_dir: Path) -> list[HITLRequest]:
    requests: list[HITLRequest] = []
    now = int(time.time())
    for json_file in watch_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text())
            nid = data.get("notification_id", json_file.stem)
            sent_at = int(data.get("sent_at", now))
            status = _status(nid, watch_dir)
            requests.append(
                HITLRequest(
                    notification_id=nid,
                    subject=str(data.get("subject", "")),
                    body=str(data.get("body", "")),
                    payload=data.get("payload", {}),
                    sent_at=sent_at,
                    status=status,
                    elapsed_seconds=now - sent_at,
                    in_progress=(watch_dir / f"{nid}.in_progress").exists(),
                )
            )
        except Exception:  # nosec B112 — intentionally skip malformed JSON
            continue

    pending = sorted(
        [
            r
            for r in requests
            if r.status == "PENDING" and not r.payload.get("is_notification_card")
        ],
        key=lambda r: r.sent_at,
    )
    return pending


def _load_last_backup() -> dict:
    """Return the most recent backup_registry row as a display dict, or empty defaults."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT backup_slug, timestamp, location FROM backup_registry"
                " WHERE deleted_from_ha_at IS NULL"
                " ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
    except Exception:
        row = None
    if not row:
        return {"slug": None, "age": "never", "location": None}
    slug, ts, location = row
    now = time.time()
    age_secs = now - (ts or now)
    age_days = age_secs / 86400
    if age_days >= 1:
        age = f"{age_days:.0f}d ago"
    elif age_secs >= 3600:
        age = f"{age_secs / 3600:.0f}h ago"
    else:
        age = f"{age_secs / 60:.0f}m ago"
    return {"slug": slug, "age": age, "location": location}


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    from utils.resource import get_resource_status
    from utils.supervisor import get_supervisor_instance
    from utils.core.timeline import load_timeline_events

    watch_dir = Path(NOTIFY_WATCH_DIR)
    watch_dir.mkdir(parents=True, exist_ok=True)
    pending_count = sum(
        1 for f in watch_dir.glob("*.json") if _status(f.stem, watch_dir) == "PENDING"
    )
    sv = get_supervisor_instance()
    loop_statuses = sv.get_statuses() if sv else []
    resource = get_resource_status()
    last_backup = _load_last_backup()
    recent_events = load_timeline_events(limit=10)
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "loop_statuses": loop_statuses,
            "resource": resource,
            "pending_count": pending_count,
            "last_backup": last_backup,
            "recent_events": recent_events,
        },
    )


@app.get("/queue", response_class=HTMLResponse)
async def queue(request: Request, order_error: str = "") -> HTMLResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    watch_dir.mkdir(parents=True, exist_ok=True)
    hitl_requests = _load_requests(watch_dir)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"requests": hitl_requests, "order_error": order_error},
    )


@app.get("/ha-status")
async def ha_status_check() -> JSONResponse:  # pragma: no cover
    """Probe the HA REST API and return reachability + version."""
    import config as _cfg
    import httpx

    url = f"http://{_cfg.HA_HOST}:{_cfg.HA_API_PORT}/api/config"
    headers = {"Authorization": f"Bearer {_cfg.HA_API_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            version = resp.json().get("version")
            return JSONResponse(
                {"reachable": True, "auth_error": False, "version": version}
            )
        if resp.status_code in (401, 403):
            return JSONResponse(
                {"reachable": True, "auth_error": True, "version": None}
            )
        return JSONResponse({"reachable": False, "auth_error": False, "version": None})
    except Exception:
        return JSONResponse({"reachable": False, "auth_error": False, "version": None})


@app.get("/events")
async def sse_events() -> StreamingResponse:
    """Server-Sent Events stream — pushes loop_status and resource events to the browser."""
    from utils.supervisor import subscribe, unsubscribe

    async def event_stream():
        q = subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _execute_queued_update(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Run the update pipeline for an approved update card."""
    import config as _config
    from agents.ha_update_manager import execute_update
    from utils.autonomy import AutonomyGate
    from utils.ha.ha_rest_client import HARestClient, UpdateStatus
    from utils.notify import get_notifier
    from utils.ha.ssh_client import AsyncSSHClient

    try:
        payload = data.get("payload", {})
        component = payload.get("component", "")
        latest_version = payload.get("latest_version", "")
        update = UpdateStatus(
            component=component,
            entity_id=payload.get("entity_id") or f"update.{component}",
            installed_version=payload.get("installed_version", ""),
            latest_version=latest_version,
            update_available=True,
            release_url=payload.get("release_url"),
            release_summary=payload.get("release_summary"),
            in_progress=False,
        )
        ssh = AsyncSSHClient()
        gate = AutonomyGate(_config.AUTONOMY_LEVEL)
        notifier = get_notifier(
            _config.NOTIFIER, _config.NOTIFY_URL, _config.NOTIFY_WATCH_DIR
        )
        ha_rest = HARestClient(
            _config.HA_HOST, _config.HA_API_PORT, _config.HA_API_TOKEN
        )
        success = await execute_update(
            update, ssh, notifier, gate, ha_rest_client=ha_rest
        )
        if success:
            data["fix_applied"] = True
            json_path.write_text(json.dumps(data, indent=2))
            (watch_dir / f"{nid}.approved").touch()
            try:
                from utils.core.timeline import write_timeline_event

                write_timeline_event(
                    "INFO",
                    "update_executor",
                    f"Update executed: {component} → {latest_version}",
                    {"component": component, "latest_version": latest_version},
                )
            except Exception:  # nosec B110  # pragma: no cover
                pass
        else:
            data["fix_error"] = (
                f"Update of {component} to {latest_version} did not complete successfully"
            )
            json_path.write_text(json.dumps(data, indent=2))
            (watch_dir / f"{nid}.rejected").touch()
            try:
                from utils.core.timeline import write_timeline_event

                write_timeline_event(
                    "ERROR",
                    "update_executor",
                    f"Update failed: {component} → {latest_version}",
                    {"component": component, "latest_version": latest_version},
                )
            except Exception:  # nosec B110  # pragma: no cover
                pass
    except Exception as exc:
        data["fix_error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                "ERROR",
                "update_executor",
                f"Update error: {component} — {exc}",
                {"component": component},
            )
        except Exception:  # nosec B110  # pragma: no cover
            pass
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


async def _post_reboot_repair_scan(settle_seconds: int = 20) -> None:
    """After a reboot succeeds, wake the repair_poll loop early to catch new issues."""
    await asyncio.sleep(settle_seconds)
    try:
        from utils.supervisor import get_supervisor_instance

        sv = get_supervisor_instance()
        if sv is not None:
            sv.run_now("repair_poll")
            log.info("post_reboot_repair_scan_triggered")
    except (
        Exception
    ) as exc:  # nosec B110 — best-effort; the scheduled poll is the fallback
        log.warning("post_reboot_repair_scan_failed", error=str(exc))


async def _execute_queued_ha_repair(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Handle an approved HA repair card — reboot or dismiss."""
    import config as _config
    from utils.ha.ha_rest_client import HARestClient, dismiss_ha_repair_issue
    from utils.notify import get_notifier
    from utils.ha.ssh_client import AsyncSSHClient

    try:
        payload = data.get("payload", {})
        action = payload.get("action", "dismiss")
        domain = payload.get("domain", "")
        issue_id = payload.get("issue_id", "")
        issue_key = payload.get("issue_key", f"{domain}/{issue_id}")

        if action == "reboot":
            from agents.ha_update_manager import execute_ha_reboot

            ssh = AsyncSSHClient()
            notifier = get_notifier(
                _config.NOTIFIER, _config.NOTIFY_URL, _config.NOTIFY_WATCH_DIR
            )
            success = await execute_ha_reboot(ssh, notifier)
            if success:
                from agents.ha_agent_advanced import mark_repair_resolved

                mark_repair_resolved(issue_key)
                data["fix_applied"] = True
                json_path.write_text(json.dumps(data, indent=2))
                (watch_dir / f"{nid}.approved").touch()
                # Wake the repair poller immediately so new post-reboot issues surface fast.
                asyncio.create_task(_post_reboot_repair_scan(settle_seconds=20))
            else:
                data["fix_error"] = "HA did not come back online after reboot"
                json_path.write_text(json.dumps(data, indent=2))
                (watch_dir / f"{nid}.rejected").touch()
        elif action == "restart":
            from agents.ha_update_manager import _poll_ha_api_ready

            ssh = AsyncSSHClient()
            await ssh.run("ha core restart")
            online = await _poll_ha_api_ready(
                _config.HA_HOST, _config.HA_API_PORT, _config.HA_API_TOKEN
            )
            if online:
                from agents.ha_agent_advanced import mark_repair_resolved

                mark_repair_resolved(issue_key)
                data["fix_applied"] = True
                json_path.write_text(json.dumps(data, indent=2))
                (watch_dir / f"{nid}.approved").touch()
                asyncio.create_task(_post_reboot_repair_scan(settle_seconds=10))
            else:
                data["fix_error"] = "HA did not come back online after Core restart"
                json_path.write_text(json.dumps(data, indent=2))
                (watch_dir / f"{nid}.rejected").touch()
        else:
            rest = HARestClient(
                _config.HA_HOST, _config.HA_API_PORT, _config.HA_API_TOKEN
            )
            await dismiss_ha_repair_issue(rest, domain, issue_id)
            from agents.ha_agent_advanced import mark_repair_resolved

            mark_repair_resolved(issue_key)
            data["fix_applied"] = True
            json_path.write_text(json.dumps(data, indent=2))
            (watch_dir / f"{nid}.approved").touch()
    except Exception as exc:
        data["fix_error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


async def _execute_netalertx_heal(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Run the NetAlertX heal action for an approved card."""
    import config as _config
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.healer import NetAlertXHealer
    from utils.autonomy import AutonomyGate
    from utils.notify import get_notifier
    from utils.ha.ssh_client import AsyncSSHClient

    (watch_dir / f"{nid}.in_progress").touch()
    try:
        payload = data.get("payload", {})
        heal_action = payload.get("heal_action", "")
        nax_ssh = AsyncSSHClient(
            _config.NETALERTX_SSH_HOST,
            _config.NETALERTX_SSH_USER,
            _config.NETALERTX_SSH_KEY_PATH,
        )
        ha_ssh = AsyncSSHClient(_config.HA_HOST, _config.HA_USER, _config.SSH_KEY_PATH)
        api = NetAlertXAPIClient(
            base_url=f"http://{_config.NETALERTX_HOST}:{_config.NETALERTX_API_PORT}",
            api_token=_config.NETALERTX_API_TOKEN,
        )
        gate = AutonomyGate(_config.AUTONOMY_LEVEL)
        notifier = get_notifier(
            _config.NOTIFIER, _config.NOTIFY_URL, _config.NOTIFY_WATCH_DIR
        )
        healer = NetAlertXHealer(
            gate=gate,
            ssh_client=nax_ssh,
            ha_ssh_client=ha_ssh,
            api_client=api,
            notifier=notifier,
            container_name=_config.NETALERTX_LOG_CONTAINER_NAME,
        )
        await healer.run_heal(heal_action)
        data["fix_applied"] = True
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.approved").touch()
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                "INFO",
                "netalertx_heal",
                f"NetAlertX heal executed: {heal_action}",
                {"heal_action": heal_action},
            )
        except Exception:  # nosec B110  # pragma: no cover
            pass
    except Exception as exc:
        data["fix_error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                "ERROR",
                "netalertx_heal",
                f"NetAlertX heal failed: {heal_action} — {exc}",
                {"heal_action": heal_action},
            )
        except Exception:  # nosec B110  # pragma: no cover
            pass
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


async def _execute_resource_action(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Run a resource management action for an approved card."""
    import sqlite3 as _sqlite3

    import config as _config
    from agents.ha_agent_advanced import (
        enforce_ha_retention,
        offload_backup_to_local,
        purge_local_backups,
    )
    from utils.resource import get_resource_status
    from utils.ha.ssh_client import AsyncSSHClient

    (watch_dir / f"{nid}.in_progress").touch()
    try:
        payload = data.get("payload", {})
        action = payload.get("action", "")
        ssh = AsyncSSHClient(_config.HA_HOST, _config.HA_USER, _config.SSH_KEY_PATH)
        _rs = get_resource_status()
        _force_critical = _rs is not None and _rs.disk_critical

        if action == "offload_backups":
            with _sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT backup_slug FROM backup_registry"
                    " WHERE location != 'both' AND deleted_from_ha_at IS NULL"
                ).fetchall()
            fail_count = 0
            for (slug,) in rows:
                ok = await offload_backup_to_local(slug, ssh_client=ssh)
                if not ok:
                    fail_count += 1
            if fail_count:
                try:
                    from utils.core.timeline import write_timeline_event

                    write_timeline_event(
                        "WARN",
                        "resource_action",
                        f"offload_backups: {fail_count}/{len(rows)} SFTP transfer(s) failed",
                        {"fail_count": fail_count, "total": len(rows)},
                    )
                except Exception:  # nosec B110
                    pass
            await enforce_ha_retention(ssh_client=ssh, force_critical=_force_critical)
            purge_local_backups()
        elif action == "enforce_retention":
            await enforce_ha_retention(ssh_client=ssh, force_critical=_force_critical)
            purge_local_backups()
        else:
            raise ValueError(f"Unknown resource action: {action!r}")

        data["fix_applied"] = True
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.approved").touch()
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                "INFO",
                "resource_action",
                f"Resource action executed: {action}",
                {"action": action},
            )
        except Exception:  # nosec B110  # pragma: no cover
            pass
    except Exception as exc:
        data["fix_error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                "ERROR",
                "resource_action",
                f"Resource action failed: {action} — {exc}",
                {"action": action},
            )
        except Exception:  # nosec B110  # pragma: no cover
            pass
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


async def _execute_disk_recovery(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Run all approved disk recovery options from a disk_recovery card."""
    import config as _config
    from agents.ha_agent_advanced import (
        enforce_ha_retention,
        offload_backup_to_local,
        purge_local_backups,
    )
    from utils.disk_recovery import (
        audit_supervisor_tmp,
        purge_recorder,
    )
    from utils.resource import get_resource_status
    from utils.ha.ssh_client import AsyncSSHClient

    (watch_dir / f"{nid}.in_progress").touch()
    actions_run: list[str] = []
    try:
        ssh = AsyncSSHClient(_config.HA_HOST, _config.HA_USER, _config.SSH_KEY_PATH)
        rest_client = None
        if _config.HA_API_TOKEN:
            from utils.ha.ha_rest_client import HARestClient

            rest_client = HARestClient(
                _config.HA_HOST, _config.HA_API_PORT, _config.HA_API_TOKEN
            )

        payload = data.get("payload", {})
        hitl_opts = payload.get("ranked_hitl_options", [])
        action_keys = {opt["action_key"] for opt in hitl_opts}

        if "repack_recorder" in action_keys and rest_client is not None:
            act = await purge_recorder(
                rest_client,
                keep_days=_config.DISK_RECOVERY_RECORDER_KEEP_DAYS,
                repack=True,
            )
            actions_run.append(f"repack_recorder: {act.message}")

        if "aggressive_purge_7d" in action_keys and rest_client is not None:
            act = await purge_recorder(rest_client, keep_days=7, repack=False)
            actions_run.append(f"aggressive_purge_7d: {act.message}")

        if "clean_tmp" in action_keys:
            items = await audit_supervisor_tmp(ssh)
            if items:
                await ssh.run("rm -rf /mnt/data/supervisor/tmp/*", check=False)
                total_bytes = sum(i.size_bytes_approx for i in items)
                actions_run.append(
                    f"clean_tmp: removed {len(items)} item(s) (~{total_bytes // (1024*1024)} MB)"
                )
            else:
                actions_run.append("clean_tmp: directory empty")

        if "offload_backups" in action_keys:
            import sqlite3 as _sqlite3

            with _sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT backup_slug FROM backup_registry"
                    " WHERE location != 'both' AND deleted_from_ha_at IS NULL"
                ).fetchall()
            for (slug,) in rows:
                await offload_backup_to_local(slug, ssh_client=ssh)
            _rs = get_resource_status()
            purged = await enforce_ha_retention(
                ssh_client=ssh,
                force_critical=_rs is not None and _rs.disk_critical,
            )
            purge_local_backups()
            actions_run.append(
                f"offload_backups: offloaded {len(rows)} backup(s), deleted {purged} from HA"
            )

        if "cleanup_orphaned_addon_dirs" in action_keys:
            orphan_opt = next(
                (
                    o
                    for o in hitl_opts
                    if o.get("action_key") == "cleanup_orphaned_addon_dirs"
                ),
                None,
            )
            orphaned_slugs = (orphan_opt or {}).get("orphaned_slugs", [])
            deleted_paths: list = []
            for entry in orphaned_slugs:
                for path in entry.get("paths", []):
                    await ssh.run(f"rm -rf {path}", check=False)
                    deleted_paths.append(path)
            actions_run.append(
                f"cleanup_orphaned_addon_dirs: removed {len(deleted_paths)} path(s)"
            )
            try:
                from utils.core.timeline import write_timeline_event as _write_ev

                _write_ev(
                    "INFO",
                    "disk_recovery",
                    f"Orphaned add-on dirs removed: {', '.join(deleted_paths)}",
                    {"deleted_paths": deleted_paths},
                )
            except Exception:  # nosec B110  # pragma: no cover
                pass

        data["fix_applied"] = True
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.approved").touch()
        try:
            from utils.core.timeline import write_timeline_event

            write_timeline_event(
                "INFO",
                "disk_recovery",
                f"Disk recovery executed: {len(actions_run)} action(s)",
                {"actions": actions_run},
            )
        except Exception:  # nosec B110  # pragma: no cover
            pass
    except Exception as exc:
        data["fix_error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


async def _execute_code_proposal(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Write approved tool code to user_tools/, import it, and register it dynamically."""
    import importlib.util
    import sys

    from utils.supervisor import get_supervisor_instance, publish_event

    payload = data.get("payload", {})
    name = payload.get("tool_name", "")
    description = payload.get("tool_description", "")
    parameters_schema = payload.get("parameters_schema", "")
    code = payload.get("code", "")

    if not name or not code:
        data["error"] = "Missing tool_name or code in payload"
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
        return

    try:
        user_tools_dir = Path(__file__).parent.parent / "user_tools"
        user_tools_dir.mkdir(exist_ok=True)
        init_file = user_tools_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("")

        tool_file = user_tools_dir / f"{name}.py"
        tool_file.write_text(code)

        module_name = f"user_tools.{name}"
        if module_name in sys.modules:
            del sys.modules[module_name]
        spec = importlib.util.spec_from_file_location(module_name, tool_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module spec for {module_name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        fn = getattr(mod, "tool_implementation", None) or getattr(mod, name, None)
        if fn is None:
            raise AttributeError(
                f"Module must define 'tool_implementation' or '{name}'"
            )

        sv = get_supervisor_instance()
        if sv is not None and sv._tool_executor is not None:
            sv._tool_executor.register_dynamic_tool(name, fn)

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO registered_tools"
                " (name, description, parameters_json, code, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, description, parameters_schema, code, time.time()),
            )

        data["tool_registered"] = True
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.approved").touch()
        publish_event({"event_type": "tool_registered", "name": name})

    except Exception as exc:
        data["error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()


async def _execute_cloud_escalation(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Re-run the HA repair agent loop with ClaudeAPIClient on approved escalation."""
    from utils.billing import BillingCapError
    from utils.autonomy import AutonomyGate
    from utils.cloud_escalation import run_cloud_escalation
    from utils.notify import get_notifier
    from utils.ha.ssh_client import AsyncSSHClient
    from utils.tool_registry import build_ha_tool_registry
    from config import AUTONOMY_LEVEL, NOTIFIER, NOTIFY_URL

    payload = data.get("payload", {})
    initial_context = payload.get("initial_context", "")
    card_id = nid

    async def _on_timeline(tool_name: str, status_line: str) -> None:
        try:
            from utils.core.timeline import write_timeline_event
            from utils.supervisor import publish_event

            write_timeline_event("INFO", "agent_loop", status_line)
            publish_event(
                {
                    "event_type": "agent_step",
                    "tool": tool_name,
                    "status": status_line,
                    "card_id": card_id,
                }
            )
        except Exception:  # nosec B110 — best-effort SSE
            pass

    try:
        ssh = AsyncSSHClient()
        notifier = get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
        gate = AutonomyGate(AUTONOMY_LEVEL)
        registry = build_ha_tool_registry()

        result = await run_cloud_escalation(
            initial_context=initial_context,
            tool_registry=registry,
            gate=gate,
            ha_ssh_client=ssh,
            notifier=notifier,
            incident_id=nid,
            timeline_callback=_on_timeline,
        )

        data["cloud_outcome"] = result.outcome
        data["cloud_steps"] = len(result.steps)
        json_path.write_text(json.dumps(data, indent=2))

        if result.outcome == "success":
            (watch_dir / f"{nid}.approved").touch()
            from utils.supervisor import publish_event as _pe

            _pe(
                {
                    "event_type": "repair_done",
                    "outcome": result.outcome,
                    "card_id": card_id,
                }
            )
        else:
            (watch_dir / f"{nid}.rejected").touch()
            from utils.supervisor import publish_event as _pe

            _pe(
                {
                    "event_type": "repair_failed",
                    "outcome": result.outcome,
                    "card_id": card_id,
                }
            )

    except BillingCapError as exc:
        data["error"] = f"Billing cap exceeded: {exc}"
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
    except Exception as exc:
        data["error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()


async def _execute_open_pr(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Apply the pending patch to the live tree, commit, push, and open a GitHub PR."""
    import subprocess  # nosec B404 — commands are allowlisted git/gh calls

    from utils.supervisor import publish_event

    payload = data.get("payload", {})
    pr_title = payload.get("pr_title", "")
    pr_body = payload.get("pr_body", "")
    branch_name = payload.get("branch_name", "")
    patch_files: dict[str, str] = payload.get("patch_files", {})

    if not pr_title or not patch_files:
        data["error"] = "Missing pr_title or patch_files in payload"
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
        return

    repo_root = Path(__file__).parent.parent
    try:
        # Ensure we're on main before branching
        subprocess.run(  # nosec B603 B607 — fixed git args, no user input in cmd
            ["git", "-C", str(repo_root), "checkout", "main"],
            check=True,
            capture_output=True,
        )
        subprocess.run(  # nosec B603 B607 — fixed git args, no user input in cmd
            ["git", "-C", str(repo_root), "pull"],
            check=True,
            capture_output=True,
        )
        subprocess.run(  # nosec B603 B607 — branch_name is agent-generated, not raw user input
            ["git", "-C", str(repo_root), "checkout", "-b", branch_name],
            check=True,
            capture_output=True,
        )

        # Apply patch files
        for rel_path, content in patch_files.items():
            target = repo_root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        # Stage and commit
        for rel_path in patch_files:
            subprocess.run(  # nosec B603 B607 — rel_path is a repo-relative path from agent
                ["git", "-C", str(repo_root), "add", rel_path],
                check=True,
                capture_output=True,
            )
        subprocess.run(  # nosec B603 B607 — pr_title is agent-generated commit message
            ["git", "-C", str(repo_root), "commit", "-m", pr_title],
            check=True,
            capture_output=True,
        )

        # Push
        subprocess.run(  # nosec B603 B607 — branch_name is agent-generated
            [
                "git",
                "-C",
                str(repo_root),
                "push",
                "-u",
                "origin",
                branch_name,
            ],
            check=True,
            capture_output=True,
        )

        # Open PR
        result = subprocess.run(  # nosec B603 B607 — gh with fixed subcommand; title/body are agent-generated strings, not shell input
            [
                "gh",
                "pr",
                "create",
                "--title",
                pr_title,
                "--body",
                pr_body,
            ],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "gh pr create failed")

        pr_url = result.stdout.strip()
        data["pr_url"] = pr_url
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.approved").touch()
        publish_event({"event_type": "pr_opened", "pr_url": pr_url, "title": pr_title})
        log.info("open_pr_executed", pr_url=pr_url, branch=branch_name)

    except Exception as exc:
        # Best-effort cleanup: switch back to main so the repo is usable
        subprocess.run(  # nosec B603 B607 — fixed args, no user input
            ["git", "-C", str(repo_root), "checkout", "main"],
            capture_output=True,
        )
        data["error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


async def _execute_netalertx_migrate(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Handle an approved NetAlertX migration card.

    Starts the uninstall supervised task (which will issue its own approval card for
    final confirmation before removing the add-on). If docker_host is configured,
    also queues the docker installer to run after uninstall completes.
    """
    import config as _cfg
    from utils.supervisor import get_supervisor_instance

    try:
        sv = get_supervisor_instance()
        if sv is not None:
            import netalertx.uninstaller as _uninstaller
            from utils.autonomy import AutonomyGate
            from utils.notify import get_notifier

            _gate = AutonomyGate(_cfg.AUTONOMY_LEVEL)
            _notifier = get_notifier(
                _cfg.NOTIFIER, _cfg.NOTIFY_URL, _cfg.NOTIFY_WATCH_DIR
            )
            sv.start(
                "netalertx_uninstall_migrate",
                lambda: _uninstaller.main(gate=_gate, notifier=_notifier),
            )
            log.info("netalertx_migrate_uninstall_started", card_id=nid)

            if _cfg.NETALERTX_DOCKER_HOST:
                import netalertx.docker_installer as _docker_installer

                sv.start(
                    "netalertx_docker_setup_migrate",
                    lambda: _docker_installer.main(gate=_gate, notifier=_notifier),
                )
                log.info("netalertx_migrate_docker_setup_queued", card_id=nid)

        (watch_dir / f"{nid}.approved").touch()
    except Exception as exc:
        log.error("netalertx_migrate_failed", error=str(exc), card_id=nid)
        data["error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


async def _execute_netalertx_switch(
    nid: str,
    data: dict,
    json_path: "Path",
    watch_dir: "Path",
) -> None:
    """Handle an approved NetAlertX switch card (HA ↔ Docker)."""
    import config as _cfg
    from utils.supervisor import get_supervisor_instance

    try:
        sv = get_supervisor_instance()
        if sv is not None:
            import netalertx.switch as _switch
            from utils.autonomy import AutonomyGate
            from utils.notify import get_notifier

            _gate = AutonomyGate(_cfg.AUTONOMY_LEVEL)
            _notifier = get_notifier(
                _cfg.NOTIFIER, _cfg.NOTIFY_URL, _cfg.NOTIFY_WATCH_DIR
            )
            sv.start(
                "netalertx_switch",
                lambda: _switch.main(gate=_gate, notifier=_notifier),
            )
            log.info("netalertx_switch_started", card_id=nid)

        (watch_dir / f"{nid}.approved").touch()
    except Exception as exc:
        log.error("netalertx_switch_failed", error=str(exc), card_id=nid)
        data["error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


async def _execute_dashboard_entity_fix(
    nid: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Handle an approved dashboard entity card — replace or remove the entity reference."""
    import config as _config

    try:
        from utils.ha.ha_rest_client import HARestClient

        payload = data.get("payload", {})
        action = payload.get("action", "investigate")
        entity_id = payload.get("entity_id", "")
        proposed = payload.get("proposed_entity_id")

        if action == "investigate":
            # Informational only — no write needed.
            data["fix_applied"] = False
            data["fix_note"] = "investigate: no Lovelace config change made"
            json_path.write_text(json.dumps(data, indent=2))
            (watch_dir / f"{nid}.approved").touch()
            return

        rest = HARestClient(_config.HA_HOST, _config.HA_API_PORT, _config.HA_API_TOKEN)
        lovelace_cfg = await rest.get_raw("/api/lovelace/config")
        cfg_json = json.dumps(lovelace_cfg)

        if action == "replace" and proposed:
            updated_json = cfg_json.replace(f'"{entity_id}"', f'"{proposed}"')
        elif action == "remove":
            # Remove any JSON string occurrence of the entity_id.
            # This handles simple "entity": "..." and entries inside "entities": [...] lists.
            updated_json = (
                cfg_json.replace(f'"{entity_id}", ', "")
                .replace(f', "{entity_id}"', "")
                .replace(f'"{entity_id}"', '""')
            )
        else:
            data["fix_note"] = f"unknown action: {action}"
            json_path.write_text(json.dumps(data, indent=2))
            (watch_dir / f"{nid}.approved").touch()
            return

        import json as _json

        updated_cfg = _json.loads(updated_json)
        await rest.post("/api/lovelace/config", updated_cfg)

        data["fix_applied"] = True
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.approved").touch()
        log.info(
            "lovelace_entity_fix_applied",
            entity_id=entity_id,
            action=action,
            proposed=proposed,
        )
    except Exception as exc:
        data["fix_error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()
    finally:
        (watch_dir / f"{nid}.in_progress").unlink(missing_ok=True)


_CARD_DISPATCH: dict[
    str,
    Any,
] = {
    CARD_TYPE_UPDATE: _execute_queued_update,
    CARD_TYPE_HA_REPAIR: _execute_queued_ha_repair,
    CARD_TYPE_NETALERTX_HEAL: _execute_netalertx_heal,
    CARD_TYPE_NETALERTX_MIGRATE: _execute_netalertx_migrate,
    CARD_TYPE_NETALERTX_SWITCH: _execute_netalertx_switch,
    CARD_TYPE_RESOURCE_ACTION: _execute_resource_action,
    CARD_TYPE_DISK_RECOVERY: _execute_disk_recovery,
    CARD_TYPE_CODE_PROPOSAL: _execute_code_proposal,
    CARD_TYPE_CLOUD_ESCALATION: _execute_cloud_escalation,
    CARD_TYPE_OPEN_PR: _execute_open_pr,
    CARD_TYPE_DASHBOARD_ENTITY: _execute_dashboard_entity_fix,
}


@app.post("/approve/{nid}")
async def approve(nid: str) -> RedirectResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        data = json.loads(json_path.read_text())
        payload = data.get("payload", {})
        card_type = payload.get("card_type", "")

        if card_type == CARD_TYPE_REPAIR or (
            not card_type and payload.get("pending_fix_yaml")
        ):
            # Repair card (typed) or legacy card with pending_fix_yaml
            yaml_content = payload.get("pending_fix_yaml", "")
            description = payload.get("pending_fix_description", "")
            (watch_dir / f"{nid}.in_progress").touch()
            asyncio.create_task(
                _execute_queued_fix(
                    nid, yaml_content, description, data, json_path, watch_dir
                )
            )
            suppression_key = payload.get("suppression_key", "")
            if suppression_key:
                from utils.hitl_tracker import mark_card_approved as _mark_approved

                with sqlite3.connect(DB_PATH) as conn:
                    _mark_approved(conn, suppression_key)
            return RedirectResponse(url="/queue", status_code=303)

        # Update ordering guard: block approving a lower-priority update if a
        # higher-priority update card is still pending.
        if card_type == CARD_TYPE_UPDATE:
            from agents.ha_update_manager import _pending_higher_priority_components

            this_component = payload.get("component", "")
            blockers = _pending_higher_priority_components(this_component, watch_dir)
            if blockers:
                blocker = blockers[0]
                return RedirectResponse(
                    url=f"/queue?order_error={blocker}", status_code=303
                )

        handler = _CARD_DISPATCH.get(card_type)
        if handler:
            (watch_dir / f"{nid}.in_progress").touch()
            asyncio.create_task(handler(nid, data, json_path, watch_dir))
        else:
            (watch_dir / f"{nid}.approved").touch()

        suppression_key = payload.get("suppression_key", "")
        if suppression_key:
            from utils.hitl_tracker import mark_card_approved as _mark_approved

            with sqlite3.connect(DB_PATH) as conn:
                _mark_approved(conn, suppression_key)
    return RedirectResponse(url="/queue", status_code=303)


async def _execute_queued_fix(
    nid: str,
    yaml_content: str,
    description: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Run the backup → sandbox → atomic-swap pipeline for a queued fix approval."""
    from agents.ha_agent_advanced import (
        enforce_ha_retention,
        offload_backup_to_local,
        purge_local_backups,
    )
    from agents.ha_agent_sandbox_engine import (
        commit_atomic_swap,
        deploy_and_test_in_sandbox,
        execute_remote_backup,
        record_backup_slug,
    )
    from utils.ha.ssh_client import AsyncSSHClient

    ssh = AsyncSSHClient()
    try:
        slug = await execute_remote_backup(ssh_client=ssh)
        record_backup_slug(slug)
        await offload_backup_to_local(slug, ssh_client=ssh)
        await enforce_ha_retention(ssh_client=ssh)
        purge_local_backups()

        passed = await deploy_and_test_in_sandbox(yaml_content, ssh_client=ssh)
        if not passed:
            data["fix_error"] = "Sandbox test failed; production not modified"
            json_path.write_text(json.dumps(data, indent=2))
            (watch_dir / f"{nid}.rejected").touch()
            return

        await commit_atomic_swap(yaml_content, ssh_client=ssh)
        data["fix_applied"] = True
        data["fix_backup_slug"] = slug
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.approved").touch()

    except Exception as exc:
        data["fix_error"] = str(exc)
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.rejected").touch()


class RepairInjectRequest(BaseModel):
    message: str


@app.post("/repair/inject")
async def repair_inject(body: RepairInjectRequest) -> JSONResponse:
    """Inject a user guidance message into the currently running repair loop.

    Returns 409 when no repair loop is active.
    """
    from utils.supervisor import get_active_repair_loop

    loop = get_active_repair_loop()
    if loop is None:
        raise HTTPException(
            status_code=409, detail="No repair loop is currently running"
        )
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message must not be empty")
    loop.inject_context(message)
    return JSONResponse({"status": "injected"})


@app.post("/reject/{nid}")
async def reject(nid: str) -> RedirectResponse:
    from config import REJECTION_COOLDOWN_HOURS
    from utils.hitl_tracker import mark_card_rejected

    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        (watch_dir / f"{nid}.rejected").touch()
        data = json.loads(json_path.read_text())
        suppression_key = data.get("payload", {}).get("suppression_key", "")
        if suppression_key:
            with sqlite3.connect(DB_PATH) as conn:
                mark_card_rejected(conn, suppression_key, REJECTION_COOLDOWN_HOURS)
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/defer/{nid}")
async def defer(nid: str) -> RedirectResponse:
    from utils.hitl_tracker import mark_card_deferred

    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        (watch_dir / f"{nid}.deferred").touch()
        # Write .rejected so wait_for_approval() unblocks with False
        (watch_dir / f"{nid}.rejected").touch()
        data = json.loads(json_path.read_text())
        suppression_key = data.get("payload", {}).get("suppression_key", "")
        if suppression_key:
            with sqlite3.connect(DB_PATH) as conn:
                mark_card_deferred(conn, suppression_key, hours=24.0)
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/suppress/{nid}")
async def suppress(nid: str) -> RedirectResponse:
    """Mark a card as a Known Issue — suppress until the condition resolves."""
    from utils.hitl_tracker import mark_card_acknowledged

    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        data = json.loads(json_path.read_text())
        suppression_key = data.get("payload", {}).get("suppression_key", "")
        if suppression_key:
            with sqlite3.connect(DB_PATH) as conn:
                mark_card_acknowledged(conn, suppression_key)
        # Remove card from queue
        (watch_dir / f"{nid}.approved").touch()
    return RedirectResponse(url="/queue", status_code=303)


@app.get("/known-issues")
async def known_issues() -> JSONResponse:
    """Return all active Known Issues as JSON."""
    from utils.hitl_tracker import get_known_issues

    with sqlite3.connect(DB_PATH) as conn:
        issues = get_known_issues(conn)
    return JSONResponse(issues)


@app.post("/known-issues/resolve/{card_key:path}")
async def resolve_known_issue(card_key: str) -> RedirectResponse:
    """Clear a Known Issue; next occurrence will start fresh."""
    from utils.hitl_tracker import mark_card_resolved

    with sqlite3.connect(DB_PATH) as conn:
        mark_card_resolved(conn, card_key)
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/dismiss-notification/{card_id}")
async def dismiss_notification(card_id: str) -> RedirectResponse:
    from config import HA_API_PORT, HA_API_TOKEN, HA_HOST
    from agents.ha_notification_manager import mark_notification_dismissed
    from utils.ha.ha_rest_client import HARestClient

    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{card_id}.json"
    if json_path.exists() and _status(card_id, watch_dir) == "PENDING":
        data = json.loads(json_path.read_text())
        ha_nid = data.get("payload", {}).get("ha_notification_id", "")
        if ha_nid:
            try:
                rest = HARestClient(HA_HOST, HA_API_PORT, HA_API_TOKEN)
                await rest.call_service(
                    "persistent_notification", "dismiss", {"notification_id": ha_nid}
                )
                mark_notification_dismissed(ha_nid, dismissed_by="user")
                (watch_dir / f"{card_id}.approved").touch()
            except Exception as exc:
                log.error(
                    "notification_dismiss_failed",
                    ha_notification_id=ha_nid,
                    error=str(exc),
                )
                return RedirectResponse(
                    url="/notifications?dismiss_error=1", status_code=303
                )
        else:
            (watch_dir / f"{card_id}.approved").touch()
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/notifications/keep/{card_id}")
async def notifications_keep(card_id: str) -> RedirectResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{card_id}.json"
    if json_path.exists() and _status(card_id, watch_dir) == "PENDING":
        (watch_dir / f"{card_id}.rejected").touch()
    return RedirectResponse(url="/notifications", status_code=303)


def _load_backup_inventory() -> list[dict]:
    """Return backup_registry rows ordered newest-first for the dashboard tab."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT backup_slug, size_bytes, timestamp, location,"
                " deleted_from_ha_at, offloaded_at, name, ha_created_at"
                " FROM backup_registry ORDER BY COALESCE(ha_created_at, timestamp) DESC"
            ).fetchall()
    except Exception:
        return []

    now = time.time()
    result = []
    for (
        slug,
        size_bytes,
        ts,
        location,
        deleted_from_ha_at,
        offloaded_at,
        name,
        ha_created_at,
    ) in rows:
        # Prefer HA-reported creation time; fall back to when Pueo recorded the backup
        age_ref = ha_created_at or ts or now
        age_secs = now - age_ref
        age_days = age_secs / 86400
        age_str = f"{age_days:.0f}d" if age_days >= 1 else f"{age_days * 24:.0f}h"
        result.append(
            {
                "slug": slug,
                "name": name or slug,
                "size_mb": round((size_bytes or 0) / (1024 * 1024), 1),
                "age": age_str,
                "on_ha": deleted_from_ha_at is None,
                "on_pueo": location in ("both", "pueo"),
                "offloaded_at": offloaded_at,
                "deleted_from_ha_at": deleted_from_ha_at,
            }
        )
    return result


@app.get("/backups", response_class=HTMLResponse)
async def backups(request: Request) -> HTMLResponse:
    inventory = _load_backup_inventory()
    return templates.TemplateResponse(
        request,
        "backups.html",
        {"inventory": inventory},
    )


@app.post("/backups/trigger")
async def trigger_backup_now() -> JSONResponse:
    """Run the full Pueo backup lifecycle and refresh the registry."""
    from agents.ha_agent_advanced import (
        enforce_ha_retention,
        execute_remote_backup,
        offload_backup_to_local,
        purge_local_backups,
        reconcile_backup_inventory,
        record_backup_slug,
    )
    from utils.ha.ssh_client import AsyncSSHClient

    ssh = AsyncSSHClient()
    try:
        await reconcile_backup_inventory(ssh_client=ssh)
        slug = await execute_remote_backup(ssh_client=ssh)
        record_backup_slug(slug)
        offloaded = await offload_backup_to_local(slug, ssh_client=ssh)
        await enforce_ha_retention(ssh_client=ssh)
        purge_local_backups()
        await reconcile_backup_inventory(ssh_client=ssh)
        return JSONResponse({"ok": True, "slug": slug, "offloaded": offloaded})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/backups/preflight")
async def run_backup_preflight() -> JSONResponse:
    """Run an on-demand update pre-flight: disk clearance and Supervisor status check."""
    from agents.ha_update_manager import run_update_preflight
    from utils.ha.ha_rest_client import HARestClient
    from utils.ha.ssh_client import AsyncSSHClient
    import config as _config

    ssh = AsyncSSHClient()
    rest = (
        HARestClient(_config.HA_HOST, _config.HA_API_PORT, _config.HA_API_TOKEN)
        if _config.HA_API_TOKEN
        else None
    )
    try:
        result = await run_update_preflight(ssh_client=ssh, rest_client=rest)
        return JSONResponse(
            {
                "ok": True,
                "disk_before_gb": round(result.disk_before_gb, 2),
                "disk_after_gb": round(result.disk_after_gb, 2),
                "backups_purged_count": result.backups_purged_count,
                "disk_ok": result.disk_ok,
                "supervisor_status": result.supervisor_status,
                "problems": result.problems,
            }
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/backups/sync")
async def sync_backup_inventory() -> JSONResponse:
    """Reconcile the local backup registry against HA and return a change summary."""
    from agents.ha_agent_advanced import reconcile_backup_inventory
    from utils.ha.ssh_client import AsyncSSHClient

    ssh = AsyncSSHClient()
    try:
        added, marked_deleted = await reconcile_backup_inventory(ssh_client=ssh)
        return JSONResponse(
            {"ok": True, "added": added, "marked_deleted": marked_deleted}
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/disk", response_class=HTMLResponse)
async def disk_tab(request: Request) -> HTMLResponse:
    import time as _time

    import utils.disk_usage as _du
    from config import HA_DISK_CRITICAL_GB, HA_DISK_WARN_GB, PUEO_LOCAL_MAX_GB
    from utils.pueo_storage import measure_pueo_footprint

    breakdown = _du.get_disk_breakdown()
    age_seconds = None
    disk_warn = False
    disk_critical = False
    if breakdown is not None:
        age_seconds = int(_time.time() - breakdown.fetched_at)
        disk_warn = breakdown.disk_free_gb < HA_DISK_WARN_GB
        disk_critical = breakdown.disk_free_gb < HA_DISK_CRITICAL_GB

    try:
        pueo_footprint = measure_pueo_footprint()
    except Exception:
        pueo_footprint = None

    return templates.TemplateResponse(
        request,
        "disk.html",
        {
            "breakdown": breakdown,
            "age_seconds": age_seconds,
            "disk_warn": disk_warn,
            "disk_critical": disk_critical,
            "pueo_footprint": pueo_footprint,
            "pueo_local_max_gb": PUEO_LOCAL_MAX_GB,
        },
    )


@app.post("/disk/queue-orphan-cleanup")
async def disk_queue_orphan_cleanup() -> JSONResponse:
    """Queue an approval card to clean up orphaned add-on data directories."""
    import config as _config
    from utils.card_types import CARD_TYPE_DISK_RECOVERY
    from utils.disk_recovery import scan_orphaned_addon_dirs
    from utils.notify import get_notifier
    from utils.ha.ssh_client import AsyncSSHClient

    ssh = AsyncSSHClient(_config.HA_HOST, _config.HA_USER, _config.SSH_KEY_PATH)
    try:
        orphans = await scan_orphaned_addon_dirs(ssh)
        if not orphans:
            return JSONResponse(
                {"ok": True, "message": "No orphaned directories found."}
            )
        total_bytes = sum(o.size_bytes for o in orphans)
        slugs = ", ".join(o.slug for o in orphans)
        notifier = get_notifier(_config.NOTIFY_WATCH_DIR)
        await notifier.send(
            subject="Pueo: orphaned add-on data cleanup requested",
            body=(
                f"Found {len(orphans)} orphaned add-on data directory(s) ({slugs}), "
                f"totalling ~{total_bytes // (1024 * 1024)} MB. "
                "Approve to delete them from the HA host."
            ),
            payload={
                "card_type": CARD_TYPE_DISK_RECOVERY,
                "type": "resource_alert",
                "severity": "INFO",
                "ranked_hitl_options": [
                    {
                        "name": "Remove orphaned app data directories",
                        "description": (
                            f"Delete leftover data directories for uninstalled apps: {slugs}."
                        ),
                        "estimated_savings": f"~{total_bytes // (1024 * 1024)} MB",
                        "risk": "LOW",
                        "action_key": "cleanup_orphaned_addon_dirs",
                        "orphaned_slugs": [
                            {
                                "slug": o.slug,
                                "paths": o.paths,
                                "size_display": o.size_display,
                            }
                            for o in orphans
                        ],
                    }
                ],
            },
        )
        return JSONResponse(
            {
                "ok": True,
                "message": f"Cleanup card queued for {len(orphans)} orphan(s).",
            }
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/disk/refresh")
async def disk_refresh() -> JSONResponse:
    import utils.disk_usage as _du
    from utils.ha.ssh_client import AsyncSSHClient

    import config as _config

    ssh = AsyncSSHClient(_config.HA_HOST, _config.HA_USER, _config.SSH_KEY_PATH)
    try:
        breakdown = await _du.fetch_disk_breakdown(ssh)
        _du.update_disk_breakdown(breakdown)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


def _load_notification_dashboard_data(
    watch_dir: Path,
    category_filter: str = "",
    severity_filter: str = "",
    sort_by: str = "first_seen_at",
) -> tuple[list[dict], list[dict]]:
    """Load notification_history rows enriched with approval card payload data.

    Returns (pending, history) where pending items have dismissed_at IS NULL.
    history contains all records (filtered and sorted).
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM notification_history ORDER BY first_seen_at DESC"
            ).fetchall()
    except Exception:
        return [], []

    records: list[dict] = []
    for row in rows:
        record: dict = dict(row)
        nid = record["notification_id"]
        card_id = f"notif_{nid}"
        json_path = watch_dir / f"{card_id}.json"
        record["card_id"] = card_id
        record["card_exists"] = json_path.exists()
        if json_path.exists():
            try:
                card_data = json.loads(json_path.read_text())
                payload = card_data.get("payload", {})
                record.setdefault(
                    "human_explanation", payload.get("human_explanation", "")
                )
                record.setdefault(
                    "recommended_action", payload.get("recommended_action", "")
                )
                record.setdefault(
                    "enriched_context", payload.get("enriched_context", {})
                )
                record.setdefault(
                    "original_message", payload.get("original_message", "")
                )
                record.setdefault("original_title", payload.get("original_title", ""))
                record.setdefault("ha_created_at", payload.get("ha_created_at"))
            except Exception:  # nosec B110 — skip malformed card JSON
                pass
        records.append(record)

    if category_filter:
        records = [r for r in records if r.get("category") == category_filter]
    if severity_filter:
        records = [r for r in records if r.get("severity") == severity_filter]

    valid_sorts = {"first_seen_at", "category", "severity"}
    sk = sort_by if sort_by in valid_sorts else "first_seen_at"
    records = sorted(
        records, key=lambda r: r.get(sk) or "", reverse=(sk == "first_seen_at")
    )

    pending = [r for r in records if not r.get("dismissed_at")]
    return pending, records


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_tab(
    request: Request,
    category: str = Query(default=""),
    severity: str = Query(default=""),
    sort_by: str = Query(default="first_seen_at"),
) -> HTMLResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    watch_dir.mkdir(parents=True, exist_ok=True)
    pending, history = _load_notification_dashboard_data(
        watch_dir,
        category_filter=category,
        severity_filter=severity,
        sort_by=sort_by,
    )
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "pending": pending,
            "history": history,
            "category_filter": category,
            "severity_filter": severity,
            "sort_by": sort_by,
        },
    )


@app.get("/timeline", response_class=HTMLResponse)
async def timeline(
    request: Request,
    page: int = Query(default=1, ge=1),
    level: str = Query(default=""),
    source: str = Query(default=""),
) -> HTMLResponse:
    from utils.core.timeline import count_timeline_events, load_timeline_events
    from config import TIMELINE_PAGE_SIZE

    offset = (page - 1) * TIMELINE_PAGE_SIZE
    events = load_timeline_events(
        limit=TIMELINE_PAGE_SIZE,
        offset=offset,
        level_filter=level,
        source_filter=source,
    )
    total = count_timeline_events(level_filter=level, source_filter=source)
    total_pages = max(1, (total + TIMELINE_PAGE_SIZE - 1) // TIMELINE_PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "events": events,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "level_filter": level,
            "source_filter": source,
        },
    )


@app.get("/timeline/{event_id}", response_class=HTMLResponse)
async def timeline_detail(request: Request, event_id: int) -> HTMLResponse:
    from fastapi.responses import Response as _Response
    from utils.core.timeline import get_timeline_event

    event = get_timeline_event(event_id)
    if event is None:
        return _Response(content="Event not found", status_code=404)  # type: ignore[return-value]
    return templates.TemplateResponse(
        request,
        "timeline_detail.html",
        {"event": event},
    )


def _build_settings_groups() -> list[dict]:
    """Return params grouped for rendering, with current config values attached."""
    import config as _config

    groups: dict[str, list[dict]] = {}
    for key, spec in _EDITABLE_PARAMS.items():
        group = spec["group"]
        if group not in groups:
            groups[group] = []
        groups[group].append(
            {
                "key": key,
                "value": getattr(_config, spec["config_attr"], ""),
                **spec,
            }
        )
    return [{"name": name, "params": params} for name, params in groups.items()]


@app.get("/settings", response_class=HTMLResponse)
async def settings_tab(request: Request) -> HTMLResponse:
    import asyncio as _asyncio
    import config as _config
    from utils.hardware import (
        detect_local_hardware,
        list_ollama_models,
        recommend_model,
    )
    from utils.service import service_status

    profile = await _asyncio.to_thread(detect_local_hardware)
    available = await _asyncio.to_thread(list_ollama_models)
    recommended = recommend_model(profile, available)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "groups": _build_settings_groups(),
            "service": service_status(),
            "api_key_set": bool(_config.ANTHROPIC_API_KEY),
            "model_info": {
                "current": _config.OLLAMA_MODEL,
                "chip": profile.chip,
                "ram_gb": profile.ram_gb,
                "recommended": recommended,
                "available": [
                    {"name": m.name, "size_gb": m.size_gb, "has_tools": m.has_tools}
                    for m in sorted(available, key=lambda m: m.size_gb, reverse=True)
                ],
            },
        },
    )


@app.post("/config")
async def update_config(req: ConfigUpdateRequest) -> JSONResponse:
    import config as _config
    import yaml as _yaml

    spec = _EDITABLE_PARAMS.get(req.key)
    if not spec:
        raise HTTPException(status_code=400, detail=f"Unknown config key: {req.key!r}")

    # Cast and validate value
    try:
        val_type = spec["val_type"]
        if val_type == "int":
            value: Any = int(req.value)
        elif val_type == "float":
            value = float(req.value)
        elif val_type == "bool":
            value = req.value.lower() in ("true", "1", "yes", "on")
        else:
            value = str(req.value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid value for {req.key}: {exc}"
        )

    if spec.get("min_val") is not None and value < spec["min_val"]:
        raise HTTPException(
            status_code=400, detail=f"{req.key} must be >= {spec['min_val']}"
        )
    if spec.get("max_val") is not None and value > spec["max_val"]:
        raise HTTPException(
            status_code=400, detail=f"{req.key} must be <= {spec['max_val']}"
        )
    if spec.get("options") and str(value) not in spec["options"]:
        raise HTTPException(
            status_code=400,
            detail=f"{req.key} must be one of {spec['options']}",
        )

    # Read → update → write config.yaml
    cfg_path = _config._config_path
    cfg: dict = {}
    if cfg_path.exists():
        cfg = _yaml.safe_load(cfg_path.read_text()) or {}

    section = spec["yaml_section"]
    if section not in cfg or not isinstance(cfg[section], dict):
        cfg[section] = {}
    cfg[section][spec["yaml_key"]] = value
    cfg_path.write_text(_yaml.dump(cfg, default_flow_style=False, allow_unicode=True))

    # Update in-memory module attribute (runtime params take effect immediately)
    setattr(_config, spec["config_attr"], value)

    # Emit SSE event so the browser can flash a confirmation
    try:
        from utils.supervisor import publish_event

        publish_event(
            {"event_type": "config_saved", "key": req.key, "new_value": value}
        )
    except (
        Exception
    ):  # nosec B110 — SSE bus may not be running in one-shot mode  # pragma: no cover
        pass  # pragma: no cover

    return JSONResponse(
        {"ok": True, "restart_required": spec.get("restart_required", False)}
    )


@app.get("/model/status")
async def model_status() -> JSONResponse:
    """Return current model, hardware profile, available models, and recommendation."""
    import asyncio as _asyncio
    import config as _config
    from utils.hardware import (
        detect_local_hardware,
        list_ollama_models,
        recommend_model,
    )

    profile = await _asyncio.to_thread(detect_local_hardware)
    available = await _asyncio.to_thread(list_ollama_models)
    recommended = recommend_model(profile, available)
    return JSONResponse(
        {
            "current_model": _config.OLLAMA_MODEL,
            "model_auto": _config.OLLAMA_MODEL_AUTO,
            "hardware": {
                "chip": profile.chip,
                "arch": profile.arch,
                "ram_gb": profile.ram_gb,
                "cpu_cores": profile.cpu_cores,
            },
            "available_models": [
                {
                    "name": m.name,
                    "size_gb": m.size_gb,
                    "has_tools": m.has_tools,
                }
                for m in sorted(available, key=lambda m: m.size_gb, reverse=True)
            ],
            "recommended": recommended,
        }
    )


@app.post("/model/switch")
async def model_switch(request: Request) -> JSONResponse:
    """Switch the active Ollama model. POST body: {model_name?: str}."""
    import asyncio as _asyncio
    import config as _config
    from utils.hardware import (
        apply_model_selection,
        detect_local_hardware,
        list_ollama_models,
        recommend_model,
    )

    body = (
        await request.json()
        if request.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    model_name: str | None = body.get("model_name") if body else None

    if model_name:
        available = await _asyncio.to_thread(list_ollama_models)
        names = [m.name for m in available]
        if model_name not in names:
            raise HTTPException(
                status_code=400,
                detail=f"Model {model_name!r} not installed. Available: {names}",
            )
        selected = model_name
    else:
        profile = await _asyncio.to_thread(detect_local_hardware)
        available = await _asyncio.to_thread(list_ollama_models)
        _candidate = recommend_model(profile, available)
        if _candidate is None:
            raise HTTPException(
                status_code=400,
                detail="No suitable tools-capable model found for this hardware.",
            )
        selected = _candidate

    previous = _config.OLLAMA_MODEL
    await _asyncio.to_thread(apply_model_selection, selected)
    return JSONResponse({"ok": True, "previous": previous, "model": selected})


@app.post("/model/refresh-cache")
async def model_refresh_cache() -> JSONResponse:
    """Re-query Ollama and update the model cache in SQLite."""
    import asyncio as _asyncio
    from agents.ha_agent_advanced import store_model_cache
    from utils.hardware import list_ollama_models

    models = await _asyncio.to_thread(list_ollama_models)
    store_model_cache(
        [
            {
                "name": m.name,
                "size_gb": m.size_gb,
                "has_tools": m.has_tools,
                "context_length": m.context_length,
                "last_seen_at": m.last_seen_at,
            }
            for m in models
        ]
    )
    return JSONResponse(
        {
            "ok": True,
            "count": len(models),
            "models": [m.name for m in models],
        }
    )


@app.post("/loops/{loop_name}/pause")
async def loop_pause(loop_name: str) -> JSONResponse:
    from utils.supervisor import get_supervisor_instance

    sv = get_supervisor_instance()
    if sv is None:
        raise HTTPException(status_code=503, detail="Supervisor not running")
    try:
        sv.pause(loop_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Loop {loop_name!r} not found")
    return JSONResponse({"ok": True})


@app.post("/loops/{loop_name}/resume")
async def loop_resume(loop_name: str) -> JSONResponse:
    from utils.supervisor import get_supervisor_instance

    sv = get_supervisor_instance()
    if sv is None:
        raise HTTPException(status_code=503, detail="Supervisor not running")
    try:
        sv.resume(loop_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Loop {loop_name!r} not found")
    return JSONResponse({"ok": True})


@app.post("/loops/{loop_name}/run-now")
async def loop_run_now(loop_name: str) -> JSONResponse:
    from utils.supervisor import get_supervisor_instance

    sv = get_supervisor_instance()
    if sv is None:
        raise HTTPException(status_code=503, detail="Supervisor not running")
    try:
        sv.run_now(loop_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Loop {loop_name!r} not found")
    return JSONResponse({"ok": True})


@app.get("/service/status")
async def service_status_endpoint() -> JSONResponse:
    from utils.service import service_status

    return JSONResponse(service_status())


@app.post("/service/install")
async def service_install() -> JSONResponse:
    from utils.service import install_service

    try:
        install_service()
        return JSONResponse({"ok": True})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/service/restart")
async def service_restart_endpoint() -> JSONResponse:
    from utils.service import restart_service

    try:
        restart_service()
        return JSONResponse({"ok": True})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/service/uninstall")
async def service_uninstall() -> JSONResponse:
    from utils.service import uninstall_service

    try:
        uninstall_service()
        return JSONResponse({"ok": True})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/service/stop")
async def service_stop() -> JSONResponse:
    from utils.service import stop_service

    try:
        stop_service()
        return JSONResponse({"ok": True})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/service/start")
async def service_start() -> JSONResponse:
    from utils.service import start_service

    try:
        start_service()
        return JSONResponse({"ok": True})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


_RUNNABLE_MODES: frozenset[str] = frozenset(
    {"audit", "update-check", "backup-status", "netalertx-diagnose", "rag-refresh"}
)

_PUEO_DIR = Path(__file__).parent.parent


@app.get("/control", response_class=HTMLResponse)
async def control_tab(request: Request) -> HTMLResponse:
    from utils.service import PLIST_TARGET, service_status

    svc = service_status()
    svc["plist_exists"] = PLIST_TARGET.exists()
    return templates.TemplateResponse(
        request,
        "control.html",
        {"service": svc, "runnable_modes": sorted(_RUNNABLE_MODES)},
    )


@app.post("/control/run")
async def control_run(mode: str = Query(...)) -> JSONResponse:
    import sys as _sys

    if mode not in _RUNNABLE_MODES:
        raise HTTPException(status_code=400, detail=f"Unknown mode: {mode!r}")

    async def _background() -> None:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable,
            str(_PUEO_DIR / "main.py"),
            "--mode",
            mode,
            cwd=str(_PUEO_DIR),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    asyncio.create_task(_background())
    return JSONResponse({"ok": True, "mode": mode})


# ── Chat routes ───────────────────────────────────────────────────────────────


def _load_chat_sessions() -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT s.id, s.created_at, s.title,"
                " COUNT(m.id) AS message_count"
                " FROM chat_sessions s"
                " LEFT JOIN chat_messages m ON m.session_id = s.id"
                " GROUP BY s.id ORDER BY s.created_at DESC"
            ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


@app.get("/chat", response_class=HTMLResponse)
async def chat_tab(request: Request) -> HTMLResponse:
    sessions = _load_chat_sessions()
    return templates.TemplateResponse(request, "chat.html", {"sessions": sessions})


@app.get("/chat/sessions")
async def chat_sessions() -> JSONResponse:
    return JSONResponse(_load_chat_sessions())


@app.get("/chat/sessions/{session_id}/messages")
async def chat_session_messages(session_id: int) -> JSONResponse:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT role, content, tool_calls_json, ts"
                " FROM chat_messages WHERE session_id = ? ORDER BY ts ASC",
                (session_id,),
            ).fetchall()
    except Exception:
        return JSONResponse([])
    return JSONResponse([dict(r) for r in rows])


@app.delete("/chat/sessions/{session_id}")
async def delete_chat_session(session_id: int) -> JSONResponse:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
            )
            conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse({}, status_code=204)


@app.get("/chat/sessions/{session_id}/debug-log")
async def chat_debug_log(session_id: int) -> Response:
    """Download a plain-text reconstruction of a chat session for debugging."""
    from utils.core.prompts import load_prompt

    try:
        system_prompt = load_prompt("agent_loop_chat")
    except Exception:
        system_prompt = "(could not load system prompt)"

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            session = conn.execute(
                "SELECT id, title, created_at FROM chat_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            messages = conn.execute(
                "SELECT role, content, tool_calls_json, ts"
                " FROM chat_messages WHERE session_id = ? ORDER BY ts ASC",
                (session_id,),
            ).fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    lines: list[str] = []
    lines.append("=== PUEO CHAT DEBUG LOG ===")
    lines.append(f"Session: {session_id}")
    lines.append(f"Title: {session['title'] or '(untitled)'}")
    created = session["created_at"]
    if created:
        lines.append(
            f"Created: {datetime.fromtimestamp(created).strftime('%Y-%m-%d %H:%M:%S')}"
        )
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("=== SYSTEM PROMPT ===")
    lines.append(system_prompt)

    lines.append("")
    lines.append("=== CONVERSATION ===")

    for msg in messages:
        role = msg["role"]
        content = msg["content"] or ""
        tool_calls_json = msg["tool_calls_json"]
        ts = msg["ts"]
        timestamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""
        lines.append("")

        if role == "user":
            lines.append(f"[User] {timestamp}")
            lines.append(content)
        elif role == "assistant" and tool_calls_json and not content:
            try:
                calls = json.loads(tool_calls_json)
                for call in calls:
                    name = call.get("name", "?")
                    args = call.get("arguments", {})
                    lines.append(f"[Tool call] {name}  {timestamp}")
                    lines.append(
                        json.dumps(args, indent=2) if args else "(no arguments)"
                    )
            except Exception:
                lines.append(f"[Tool call – parse error] {tool_calls_json}")
        elif role == "tool":
            lines.append("[Tool result]")
            lines.append(content)
        elif role == "assistant" and content:
            lines.append(f"[Assistant] {timestamp}")
            lines.append(content)

    text = "\n".join(lines) + "\n"
    filename = f"pueo-chat-{session_id}-debug.txt"
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/chat/events")
async def chat_sse_events() -> StreamingResponse:
    """SSE stream for chat progress: chat_thinking, chat_done, chat_error."""
    from utils.supervisor import subscribe_chat, unsubscribe_chat

    async def event_stream():
        q = subscribe_chat()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe_chat(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ChatMessageRequest(BaseModel):
    session_id: int | None = None
    message: str


@app.post("/chat/message")
async def post_chat_message(req: ChatMessageRequest) -> JSONResponse:
    """Accept a user message, persist it, and dispatch an async agent loop."""
    session_id = req.session_id
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            if session_id is None:
                title = message[:60]
                cur = conn.execute(
                    "INSERT INTO chat_sessions (created_at, title) VALUES (?, ?)",
                    (time.time(), title),
                )
                session_id = cur.lastrowid

            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, ts)"
                " VALUES (?, ?, ?, ?)",
                (session_id, "user", message, time.time()),
            )
            history = conn.execute(
                "SELECT role, content FROM chat_messages"
                " WHERE session_id = ? ORDER BY ts ASC",
                (session_id,),
            ).fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    prior = [{"role": r, "content": c} for r, c in history[:-1]]
    if session_id is None:
        raise HTTPException(status_code=500, detail="session creation failed")
    asyncio.create_task(_run_chat_loop(session_id, message, prior))
    return JSONResponse({"session_id": session_id}, status_code=202)


_REDACTED_ARG_KEYS = frozenset({"key", "token", "password", "secret", "credential"})


def _sanitize_args_preview(args: dict) -> str:
    parts = []
    for k, v in args.items():
        if any(r in k.lower() for r in _REDACTED_ARG_KEYS):
            parts.append(f"{k}=***")
        else:
            parts.append(f"{k}={str(v)[:40]}")
    preview = ", ".join(parts)
    return preview[:120] if len(preview) > 120 else preview


async def _run_chat_loop(
    session_id: int,
    message: str,
    history: list[dict],
) -> None:
    """Run one AgentLoop turn for a chat session and publish SSE progress events."""
    from config import (
        AUTONOMY_LEVEL,
        HA_HOST,
        HA_USER,
        NOTIFIER,
        NOTIFY_URL,
        NOTIFY_WATCH_DIR,
        SSH_KEY_PATH,
    )
    from utils.agent_loop import AgentLoop, _CHAT_SYSTEM_PROMPT
    from utils.autonomy import AutonomyGate
    from utils.notify import get_notifier
    from utils.llm.llm_factory import make_llm_client
    from utils.supervisor import get_supervisor_instance, publish_chat_event
    from utils.tool_executor import ToolExecutor
    from utils.tool_registry import AgentStep, ToolCall, build_chat_tool_registry

    sv = get_supervisor_instance()
    executor: ToolExecutor
    if (
        sv is not None
        and hasattr(sv, "_tool_executor")
        and sv._tool_executor is not None
    ):
        executor = sv._tool_executor
    else:
        from interfaces import SSHClientProtocol

        ssh: SSHClientProtocol
        try:
            from utils.ha.ssh_client import AsyncSSHClient

            ssh = AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
        except Exception:
            from utils.ha.ssh_client import FakeSSHClient

            ssh = FakeSSHClient()
        gate = AutonomyGate(AUTONOMY_LEVEL)
        notifier = get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
        executor = ToolExecutor(ha_ssh_client=ssh, gate=gate, notifier=notifier)

    async def on_pre_step(tool_call: ToolCall) -> None:
        publish_chat_event(
            {
                "event_type": "chat_pre_step",
                "session_id": session_id,
                "tool": tool_call.name,
                "args_preview": _sanitize_args_preview(tool_call.arguments),
            }
        )

    def on_step(step: AgentStep) -> None:
        publish_chat_event(
            {
                "event_type": "chat_thinking",
                "session_id": session_id,
                "tool": step.tool_call.name,
                "step": step.step_number,
            }
        )
        output_summary = (step.tool_result.output or step.tool_result.error or "")[:300]
        publish_chat_event(
            {
                "event_type": "chat_step_result",
                "session_id": session_id,
                "tool": step.tool_call.name,
                "step": step.step_number,
                "output_summary": output_summary,
                "success": step.tool_result.success,
            }
        )

    # Pass prior turns as structured messages so the model has proper
    # multi-turn context rather than a flattened text block.
    prior_messages = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in history
        if m.get("role") in ("user", "assistant")
    ]

    try:
        agent_loop = AgentLoop(
            llm_client=make_llm_client(),
            tool_executor=executor,
            tool_registry=build_chat_tool_registry(),
            system_prompt=_CHAT_SYSTEM_PROMPT,
            terminal_tool_name="finish_chat",
            max_tool_calls=10,
            max_wall_seconds=AGENT_MAX_WALL_SECONDS,
            step_callback=on_step,
            pre_step_callback=on_pre_step,
        )
        result = await agent_loop.run(message, initial_messages=prior_messages or None)

        summary = result.episode_stub.get("summary", "") if result.episode_stub else ""
        if not summary:
            for step in reversed(result.steps):
                if step.tool_call.name == "finish_chat":
                    summary = step.tool_call.arguments.get("summary", "")
                    break
        if not summary:
            if result.outcome == "timeout":
                completed = [s.tool_call.name for s in result.steps]
                summary = (
                    f"Timed out after {len(result.steps)} step(s): "
                    f"{', '.join(completed) or 'none completed'}. "
                    "Check /backups to see current backup state."
                )
            else:
                summary = f"(Loop ended: {result.outcome})"

        with sqlite3.connect(DB_PATH) as conn:
            for step in result.steps:
                if step.tool_call.name == "finish_chat":
                    continue
                conn.execute(
                    "INSERT INTO chat_messages"
                    " (session_id, role, content, tool_calls_json, ts)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        "assistant",
                        "",
                        json.dumps(
                            [
                                {
                                    "name": step.tool_call.name,
                                    "arguments": step.tool_call.arguments,
                                }
                            ]
                        ),
                        step.timestamp,
                    ),
                )
                output = (step.tool_result.output or step.tool_result.error or "")[:500]
                conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, ts)"
                    " VALUES (?, ?, ?, ?)",
                    (session_id, "tool", output, step.timestamp + 0.0005),
                )
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, ts)"
                " VALUES (?, ?, ?, ?)",
                (session_id, "assistant", summary, time.time()),
            )

        publish_chat_event(
            {
                "event_type": "chat_done",
                "session_id": session_id,
                "content": summary,
            }
        )
    except Exception as exc:
        publish_chat_event(
            {
                "event_type": "chat_error",
                "session_id": session_id,
                "error": str(exc),
            }
        )


@app.get("/episodes", response_class=HTMLResponse)
async def episodes_tab(
    request: Request,
    trigger: str = Query(default=""),
    outcome: str = Query(default=""),
) -> HTMLResponse:
    from utils.repair_episode import load_episodes

    episodes = load_episodes(DB_PATH)
    if trigger:
        episodes = [ep for ep in episodes if ep.trigger == trigger]
    if outcome == "passed":
        episodes = [ep for ep in episodes if ep.verification_result]
    elif outcome == "failed":
        episodes = [ep for ep in episodes if not ep.verification_result]
    return templates.TemplateResponse(
        request,
        "episodes.html",
        {
            "episodes": episodes,
            "filter_trigger": trigger,
            "filter_outcome": outcome,
        },
    )


@app.get("/episodes/export")
async def export_episodes(
    since: str = Query(default=""),
) -> StreamingResponse:
    """Return anonymized YAML of all episodes (optionally filtered by --since date)."""
    from datetime import datetime

    from utils.repair_episode import export_episodes_yaml, load_episodes

    since_ts = None
    if since:
        try:
            since_ts = datetime.fromisoformat(since).timestamp()
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date: {since!r}")
    episodes = load_episodes(DB_PATH, since=since_ts)
    content = export_episodes_yaml(episodes) if episodes else "# No episodes found.\n"
    return StreamingResponse(
        iter([content]),
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=repair-episodes.yaml"},
    )


@app.get("/episodes/{episode_id}/prepare", response_class=HTMLResponse)
async def prepare_episode_submission(
    request: Request,
    episode_id: str,
) -> HTMLResponse:
    """Render review page for submitting an episode to the federated case library."""
    from utils.repair_episode import export_single_episode_yaml, load_episode

    episode = load_episode(DB_PATH, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id!r} not found")

    yaml_preview = export_single_episode_yaml(episode)
    return templates.TemplateResponse(
        request,
        "episode_prepare.html",
        {
            "episode": episode,
            "yaml_preview": yaml_preview,
            "cases_repo": FEDERATED_CASES_REPO,
            "repo_configured": bool(FEDERATED_CASES_REPO),
        },
    )


class EpisodeSubmitRequest(BaseModel):
    description: str = ""


@app.post("/episodes/{episode_id}/submit")
async def submit_episode_to_case_library(
    episode_id: str,
    body: EpisodeSubmitRequest,
) -> JSONResponse:
    """
    Clone FEDERATED_CASES_REPO, commit episode YAML on a new branch, open a PR.

    Returns JSON with 'pr_url' on success or 'error' on failure.
    """
    from utils.case_submitter import CaseSubmitError, submit_episode
    from utils.repair_episode import (
        export_single_episode_yaml,
        load_episode,
        mark_episode_submitted,
    )

    if not FEDERATED_CASES_REPO:
        raise HTTPException(
            status_code=400,
            detail="FEDERATED_CASES_REPO is not configured. "
            "Add 'federated_cases_repo: owner/pueo-cases' under 'agent:' in config.yaml.",
        )

    episode = load_episode(DB_PATH, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail=f"Episode {episode_id!r} not found")

    if episode.submitted_at is not None:
        return JSONResponse({"pr_url": episode.pr_url, "already_submitted": True})

    yaml_content = export_single_episode_yaml(episode, body.description)
    pr_title = f"Repair episode: {episode.trigger} / {episode.id[:8]}"
    pr_body_lines = [
        f"Anonymized repair episode from Pueo.",
        f"",
        f"- **Trigger:** {episode.trigger}",
        f"- **Outcome:** {'passed' if episode.verification_result else 'failed'}",
        f"- **Tools used:** {len(episode.tool_sequence)}",
        f"- **Duration:** {episode.duration_seconds:.1f}s",
    ]
    if body.description:
        pr_body_lines = [body.description, ""] + pr_body_lines
    pr_body_lines += [
        "",
        "```yaml",
        yaml_content.strip(),
        "```",
    ]
    pr_body = "\n".join(pr_body_lines)

    try:
        pr_url = await submit_episode(
            episode_id=episode.id,
            yaml_content=yaml_content,
            cases_repo=FEDERATED_CASES_REPO,
            pr_title=pr_title,
            pr_body=pr_body,
        )
        mark_episode_submitted(DB_PATH, episode.id, pr_url)
        log.info("episode_submitted", episode_id=episode.id, pr_url=pr_url)
        return JSONResponse({"pr_url": pr_url, "already_submitted": False})
    except CaseSubmitError as exc:
        log.error("episode_submit_failed", episode_id=episode.id, error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)


def run_dashboard() -> None:
    import uvicorn

    print(f"Pueo Dashboard → http://localhost:{DASHBOARD_PORT}")
    uvicorn.run(
        app,
        host="0.0.0.0",  # nosec B104 — local dashboard, binding all interfaces is intentional
        port=DASHBOARD_PORT,
        log_level="warning",
    )
