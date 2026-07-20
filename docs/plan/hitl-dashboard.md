# HITL Web Dashboard — Item 19.5

Part of the [Implementation Plan](../implementation-plan.md) · Phase 4.5 · 1 session.

---

### 19.5. HITL Web Dashboard ✅ Done (2026-07-20) — PR #33

**Problem:** `AutonomyGate.require_approval()` blocked the entire monitoring loop via `asyncio.wait_for()` for up to 60 minutes waiting for the user to touch a `.approved` file. This was doubly broken: (a) the monitor stopped processing new log events while waiting, and (b) the 60-minute timeout caused silent skip — a repair that needed human attention was silently abandoned with no feedback to the user. The replacement: eliminate the timeout entirely, make monitoring loops fire healing as a background task so they stay alive, and add a local web dashboard that queues issues with full context and lets the user approve or reject via browser at their own pace.

**Built:**

**1. Remove HITL timeout**
- Deleted `HITL_TIMEOUT_MINUTES` from `config.py`, `config.yaml.default`, and `setup.sh`.
- Removed `asyncio.wait_for()` wrapper from `AutonomyGate.require_approval()`. The gate now calls `notifier.wait_for_approval(nid)` directly and polls indefinitely. `FileNotifier.wait_for_approval()` already yields via `asyncio.sleep()` so the event loop is never starved.
- Removed `timeout_minutes` parameter from `AutonomyGate.__init__()` and all four call sites (`ha_agent_sandbox_engine.py`, `ha_log_monitor.py`, `netalertx/log_monitor.py`, `netalertx/installer.py`).

**2. Non-blocking monitoring loops**
- `ha_log_monitor.py` and `netalertx/log_monitor.py` now fire healing via `asyncio.create_task()` instead of `await`. The log ingestion loop continues processing new events while a repair waits for human approval.
- The installer (`netalertx/installer.py`) is a one-shot sequential state machine and continues to `await` inline — no change needed there.

**3. New `web/` package**
- `web/dashboard.py` — FastAPI app with:
  - `HITLRequest` Pydantic model (`notification_id`, `subject`, `body`, `payload`, `sent_at`, `status`, `elapsed_seconds`; status validated to `PENDING | APPROVED | REJECTED`)
  - `_status(nid, watch_dir)` — checks for `.approved`/`.rejected` signal files
  - `_load_requests(watch_dir)` — reads all `.json` files, skips malformed, sorts PENDING first (oldest→newest) then resolved (newest→oldest)
  - `GET /` — renders the card list
  - `POST /approve/{nid}` and `POST /reject/{nid}` — validate `.json` exists and status is PENDING, touch signal file, redirect 303
  - `run_dashboard()` — called by `main.py`; uvicorn manages its own event loop (not `asyncio.run()`)
- `web/templates/base.html` — HTML5 shell, inline CSS, `<meta http-equiv="refresh" content="10">`
- `web/templates/index.html` — card list: status badge, subject, elapsed time, body, severity/risk/step meta, collapsible JSON payload, Approve/Reject forms (only rendered when PENDING)

**4. New config key: `DASHBOARD_PORT`**
- `config.py`: `DASHBOARD_PORT: int = int(_agent.get("dashboard_port", 8080))`
- `config.yaml.default`: `dashboard_port: 8080` under `agent:` (replaces `hitl_timeout_minutes`)
- `setup.sh`: `ask "HITL dashboard port" "8080" DASHBOARD_PORT`; `dashboard_port: ${DASHBOARD_PORT}` in config heredoc; `"  HITL dashboard       : python main.py --mode dashboard"` in Done block

**5. `main.py`**
- Added `"dashboard"` to choices and epilog
- Dispatch: `from web.dashboard import run_dashboard; run_dashboard()`

**New dependencies:** `fastapi`, `jinja2`, `uvicorn` added to `requirements.txt`.

**Tests (`tests/test_core.py`):**
- Deleted `test_hitl_timeout_minutes_default`, `test_hitl_timeout_minutes_from_yaml`, `test_require_approval_timeout_returns_false`
- Added `test_require_approval_waits_until_approved` and `test_require_approval_waits_until_rejected` to `TestAutonomyGate`
- Added `TestDashboardConfig`: default port, port from yaml
- Added `TestHITLRequestModel`: valid construction, invalid status raises, missing field raises, JSON round-trip
- Added `TestLoadHITLRequests`: pending, approved, rejected, empty dir, orphan signal files, malformed JSON, pending sorted before resolved
- Added `TestMainDashboardMode`: `"dashboard"` in `--help` output

**Done criteria met:**
- `python main.py --mode dashboard` serves `http://localhost:8080` with empty queue
- Dropping a `.json` into `hitl/` shows it as PENDING within 10s auto-refresh
- Approve/Reject buttons write signal files and flip card status
- `--mode monitor` continues processing log lines while a repair awaits approval
- Full CI gate passes: black, flake8, mypy, bandit, pytest --cov (508 tests, 96% coverage)
