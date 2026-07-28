"""HITL web dashboard — queue approval/rejection of pending repair actions."""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from config import DASHBOARD_PORT, NOTIFY_WATCH_DIR, DB_PATH

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


class HITLRequest(BaseModel):
    notification_id: str
    subject: str
    body: str
    payload: dict[str, Any]
    sent_at: int
    status: str
    elapsed_seconds: int

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
                )
            )
        except Exception:  # nosec B112 — intentionally skip malformed JSON
            continue

    pending = sorted(
        [r for r in requests if r.status == "PENDING"],
        key=lambda r: r.sent_at,
    )
    resolved = sorted(
        [r for r in requests if r.status != "PENDING"],
        key=lambda r: r.sent_at,
        reverse=True,
    )
    return pending + resolved


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    watch_dir.mkdir(parents=True, exist_ok=True)
    hitl_requests = _load_requests(watch_dir)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"requests": hitl_requests},
    )


@app.post("/approve/{nid}")
async def approve(nid: str) -> RedirectResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        data = json.loads(json_path.read_text())
        pending_yaml = data.get("payload", {}).get("pending_fix_yaml")
        if pending_yaml:
            description = data.get("payload", {}).get("pending_fix_description", "")
            await _execute_queued_fix(
                nid, pending_yaml, description, data, json_path, watch_dir
            )
            return RedirectResponse(url="/", status_code=303)
        (watch_dir / f"{nid}.approved").touch()
    return RedirectResponse(url="/", status_code=303)


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
    return RedirectResponse(url="/", status_code=303)


@app.post("/defer/{nid}")
async def defer(nid: str) -> RedirectResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        (watch_dir / f"{nid}.deferred").touch()
        # Write .rejected so wait_for_approval() unblocks with False
        (watch_dir / f"{nid}.rejected").touch()
    return RedirectResponse(url="/", status_code=303)


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
            except Exception:  # nosec B110
                pass
        (watch_dir / f"{card_id}.approved").touch()
    return RedirectResponse(url="/", status_code=303)


def _load_backup_inventory() -> list[dict]:
    """Return backup_registry rows ordered newest-first for the dashboard tab."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT backup_slug, size_bytes, timestamp, location,"
                " deleted_from_ha_at, offloaded_at"
                " FROM backup_registry ORDER BY timestamp DESC"
            ).fetchall()
    except Exception:
        return []

    now = time.time()
    result = []
    for slug, size_bytes, ts, location, deleted_from_ha_at, offloaded_at in rows:
        age_secs = now - (ts or now)
        age_days = age_secs / 86400
        age_str = f"{age_days:.0f}d" if age_days >= 1 else f"{age_days * 24:.0f}h"
        result.append(
            {
                "slug": slug,
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


def run_dashboard() -> None:
    import uvicorn

    print(f"Pueo HITL Dashboard → http://localhost:{DASHBOARD_PORT}")
    uvicorn.run(
        app,
        host="0.0.0.0",  # nosec B104 — local dashboard, binding all interfaces is intentional
        port=DASHBOARD_PORT,
        log_level="warning",
    )
