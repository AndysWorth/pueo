"""HITL web dashboard — queue approval/rejection of pending repair actions."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from config import DASHBOARD_PORT, NOTIFY_WATCH_DIR

app = FastAPI(title="Pueo HITL Dashboard")
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
        if v not in {"PENDING", "APPROVED", "REJECTED"}:
            raise ValueError(f"unknown status: {v}")
        return v


def _status(nid: str, watch_dir: Path) -> str:
    if (watch_dir / f"{nid}.approved").exists():
        return "APPROVED"
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
        (watch_dir / f"{nid}.approved").touch()
    return RedirectResponse(url="/", status_code=303)


@app.post("/reject/{nid}")
async def reject(nid: str) -> RedirectResponse:
    watch_dir = Path(NOTIFY_WATCH_DIR)
    json_path = watch_dir / f"{nid}.json"
    if json_path.exists() and _status(nid, watch_dir) == "PENDING":
        (watch_dir / f"{nid}.rejected").touch()
    return RedirectResponse(url="/", status_code=303)


def run_dashboard() -> None:
    import uvicorn

    print(f"Pueo HITL Dashboard → http://localhost:{DASHBOARD_PORT}")
    uvicorn.run(
        app,
        host="0.0.0.0",  # nosec B104 — local dashboard, binding all interfaces is intentional
        port=DASHBOARD_PORT,
        log_level="warning",
    )
