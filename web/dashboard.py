"""HITL web dashboard — queue approval/rejection of pending repair actions."""

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
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from config import AGENT_MAX_WALL_SECONDS, DASHBOARD_PORT, NOTIFY_WATCH_DIR, DB_PATH
from utils.logging import get_logger
from utils.card_types import (
    CARD_TYPE_CODE_PROPOSAL,
    CARD_TYPE_HA_REPAIR,
    CARD_TYPE_NETALERTX_HEAL,
    CARD_TYPE_REPAIR,
    CARD_TYPE_RESOURCE_ACTION,
    CARD_TYPE_UPDATE,
)

log = get_logger("dashboard")

app = FastAPI(title="Pueo HITL Dashboard")
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
    "hitl_always": {
        "yaml_section": "agent",
        "yaml_key": "hitl_always",
        "config_attr": "HITL_ALWAYS",
        "val_type": "bool",
        "description": "Force HITL approval for every action regardless of autonomy level",
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
        "description": "HITL delivery method",
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
    from utils.timeline import load_timeline_events

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
    """Run the update pipeline for a HITL-approved update card."""
    import config as _config
    from ha_update_manager import execute_update
    from utils.autonomy import AutonomyGate
    from utils.ha_rest_client import HARestClient, UpdateStatus
    from utils.notify import get_notifier
    from utils.ssh_client import AsyncSSHClient

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
                from utils.timeline import write_timeline_event

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
                from utils.timeline import write_timeline_event

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
            from utils.timeline import write_timeline_event

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
    from utils.ha_rest_client import HARestClient, dismiss_ha_repair_issue
    from utils.notify import get_notifier
    from utils.ssh_client import AsyncSSHClient

    try:
        payload = data.get("payload", {})
        action = payload.get("action", "dismiss")
        domain = payload.get("domain", "")
        issue_id = payload.get("issue_id", "")
        issue_key = payload.get("issue_key", f"{domain}/{issue_id}")

        if action == "reboot":
            from ha_update_manager import execute_ha_reboot

            ssh = AsyncSSHClient()
            notifier = get_notifier(
                _config.NOTIFIER, _config.NOTIFY_URL, _config.NOTIFY_WATCH_DIR
            )
            success = await execute_ha_reboot(ssh, notifier)
            if success:
                from ha_agent_advanced import mark_repair_resolved

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
        else:
            rest = HARestClient(
                _config.HA_HOST, _config.HA_API_PORT, _config.HA_API_TOKEN
            )
            await dismiss_ha_repair_issue(rest, domain, issue_id)
            from ha_agent_advanced import mark_repair_resolved

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
    """Run the NetAlertX heal action for a HITL-approved card."""
    import config as _config
    from netalertx.api_client import NetAlertXAPIClient
    from netalertx.healer import NetAlertXHealer
    from utils.autonomy import AutonomyGate
    from utils.notify import get_notifier
    from utils.ssh_client import AsyncSSHClient

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
            from utils.timeline import write_timeline_event

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
            from utils.timeline import write_timeline_event

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
    """Run a resource management action for a HITL-approved card."""
    import sqlite3 as _sqlite3

    import config as _config
    from ha_agent_advanced import (
        enforce_ha_retention,
        offload_backup_to_local,
        purge_local_backups,
    )
    from utils.ssh_client import AsyncSSHClient

    (watch_dir / f"{nid}.in_progress").touch()
    try:
        payload = data.get("payload", {})
        action = payload.get("action", "")
        ssh = AsyncSSHClient(_config.HA_HOST, _config.HA_USER, _config.SSH_KEY_PATH)

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
                    from utils.timeline import write_timeline_event

                    write_timeline_event(
                        "WARN",
                        "resource_action",
                        f"offload_backups: {fail_count}/{len(rows)} SFTP transfer(s) failed",
                        {"fail_count": fail_count, "total": len(rows)},
                    )
                except Exception:  # nosec B110
                    pass
            await enforce_ha_retention(ssh_client=ssh)
            purge_local_backups()
        elif action == "enforce_retention":
            await enforce_ha_retention(ssh_client=ssh)
            purge_local_backups()
        else:
            raise ValueError(f"Unknown resource action: {action!r}")

        data["fix_applied"] = True
        json_path.write_text(json.dumps(data, indent=2))
        (watch_dir / f"{nid}.approved").touch()
        try:
            from utils.timeline import write_timeline_event

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
            from utils.timeline import write_timeline_event

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


_CARD_DISPATCH: dict[
    str,
    Any,
] = {
    CARD_TYPE_UPDATE: _execute_queued_update,
    CARD_TYPE_HA_REPAIR: _execute_queued_ha_repair,
    CARD_TYPE_NETALERTX_HEAL: _execute_netalertx_heal,
    CARD_TYPE_RESOURCE_ACTION: _execute_resource_action,
    CARD_TYPE_CODE_PROPOSAL: _execute_code_proposal,
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
            return RedirectResponse(url="/queue", status_code=303)

        # Update ordering guard: block approving a lower-priority update if a
        # higher-priority update card is still pending.
        if card_type == CARD_TYPE_UPDATE:
            from ha_update_manager import _pending_higher_priority_components

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
    return RedirectResponse(url="/queue", status_code=303)


async def _execute_queued_fix(
    nid: str,
    yaml_content: str,
    description: str,
    data: dict,
    json_path: Path,
    watch_dir: Path,
) -> None:
    """Run the backup → sandbox → atomic-swap pipeline for a queued HITL fix."""
    from ha_agent_advanced import (
        enforce_ha_retention,
        offload_backup_to_local,
        purge_local_backups,
    )
    from ha_agent_sandbox_engine import (
        commit_atomic_swap,
        deploy_and_test_in_sandbox,
        execute_remote_backup,
        record_backup_slug,
    )
    from utils.ssh_client import AsyncSSHClient

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


@app.post("/reject/{nid}")
async def reject(nid: str) -> RedirectResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        (watch_dir / f"{nid}.rejected").touch()
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/defer/{nid}")
async def defer(nid: str) -> RedirectResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        (watch_dir / f"{nid}.deferred").touch()
        # Write .rejected so wait_for_approval() unblocks with False
        (watch_dir / f"{nid}.rejected").touch()
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/dismiss-notification/{card_id}")
async def dismiss_notification(card_id: str) -> RedirectResponse:
    from config import HA_API_PORT, HA_API_TOKEN, HA_HOST
    from ha_notification_manager import mark_notification_dismissed
    from utils.ha_rest_client import HARestClient

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
    from ha_agent_advanced import (
        enforce_ha_retention,
        execute_remote_backup,
        offload_backup_to_local,
        purge_local_backups,
        reconcile_backup_inventory,
        record_backup_slug,
    )
    from utils.ssh_client import AsyncSSHClient

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
    from ha_update_manager import run_update_preflight
    from utils.ha_rest_client import HARestClient
    from utils.ssh_client import AsyncSSHClient
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
    from ha_agent_advanced import reconcile_backup_inventory
    from utils.ssh_client import AsyncSSHClient

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
    from config import HA_DISK_CRITICAL_GB, HA_DISK_WARN_GB

    breakdown = _du.get_disk_breakdown()
    age_seconds = None
    disk_warn = False
    disk_critical = False
    if breakdown is not None:
        age_seconds = int(_time.time() - breakdown.fetched_at)
        disk_warn = breakdown.disk_free_gb < HA_DISK_WARN_GB
        disk_critical = breakdown.disk_free_gb < HA_DISK_CRITICAL_GB
    return templates.TemplateResponse(
        request,
        "disk.html",
        {
            "breakdown": breakdown,
            "age_seconds": age_seconds,
            "disk_warn": disk_warn,
            "disk_critical": disk_critical,
        },
    )


@app.post("/disk/refresh")
async def disk_refresh() -> JSONResponse:
    import utils.disk_usage as _du
    from utils.ssh_client import AsyncSSHClient

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
    """Load notification_history rows enriched with HITL card payload data.

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
    from utils.timeline import count_timeline_events, load_timeline_events
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
    from utils.timeline import get_timeline_event

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
    from utils.service import service_status

    return templates.TemplateResponse(
        request,
        "settings.html",
        {"groups": _build_settings_groups(), "service": service_status()},
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
    from utils.ollama_client import OllamaClient
    from utils.supervisor import get_supervisor_instance, publish_chat_event
    from utils.tool_executor import ToolExecutor
    from utils.tool_registry import AgentStep, build_chat_tool_registry

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
            from utils.ssh_client import AsyncSSHClient

            ssh = AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
        except Exception:
            from utils.ssh_client import FakeSSHClient

            ssh = FakeSSHClient()
        gate = AutonomyGate(AUTONOMY_LEVEL)
        notifier = get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)
        executor = ToolExecutor(ha_ssh_client=ssh, gate=gate, notifier=notifier)

    def on_step(step: AgentStep) -> None:
        publish_chat_event(
            {
                "event_type": "chat_thinking",
                "session_id": session_id,
                "tool": step.tool_call.name,
                "step": step.step_number,
            }
        )

    context_parts: list[str] = []
    if history:
        context_parts.append("[Prior conversation]")
        for msg in history:
            role = msg["role"]
            content = (msg.get("content") or "").strip()
            if role == "user":
                context_parts.append(f"User: {content}")
            elif role == "assistant":
                context_parts.append(f"Pueo: {content}")
        context_parts.append("")
    context_parts.append(message)
    initial_context = "\n".join(context_parts)

    try:
        agent_loop = AgentLoop(
            llm_client=OllamaClient(),
            tool_executor=executor,
            tool_registry=build_chat_tool_registry(),
            system_prompt=_CHAT_SYSTEM_PROMPT,
            terminal_tool_name="finish_chat",
            max_tool_calls=10,
            max_wall_seconds=AGENT_MAX_WALL_SECONDS,
            step_callback=on_step,
        )
        result = await agent_loop.run(initial_context)

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


def run_dashboard() -> None:
    import uvicorn

    print(f"Pueo HITL Dashboard → http://localhost:{DASHBOARD_PORT}")
    uvicorn.run(
        app,
        host="0.0.0.0",  # nosec B104 — local dashboard, binding all interfaces is intentional
        port=DASHBOARD_PORT,
        log_level="warning",
    )
