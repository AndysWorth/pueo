# Plan: Pueo Supervisor — Unified Process + Active Dashboard

## Milestone 6.5

**Status:** TODO — Phase 17 (next to implement)

---

## Problem

Pueo currently has to be run as a collection of disconnected one-shot and daemon commands.
In practice only `--mode dashboard` tends to stay running, and it is passive: it serves the
web UI but cannot execute approved actions or run any background monitoring. The result (from
`audits/ha-operational-state-2026-07-29.md`) is that:

- Approved HITL actions (e.g. the pending HA Core 2026.7.4 update) never execute because
  the process that sent the card has exited before the user approves.
- No monitoring loops run: disk is at CRITICAL with no alert sent, pycync errors go unnoticed,
  NetAlertX has never scanned.
- There is no process supervisor keeping Pueo alive between sessions.

---

## Goal

`python main.py` (no flags) starts Pueo in supervisor mode. A single asyncio process:

1. Runs all monitoring loops concurrently as asyncio tasks.
2. Serves the FastAPI dashboard (which becomes the user-facing face of Pueo).
3. Executes HITL-approved actions directly in-process, rather than relying on a
   now-exited process to pick up a `.approved` file.
4. Stays alive via a launchd plist that restarts it on crash or after login.

One-shot `--mode <x>` commands remain available for debugging and manual triggers.

---

## Architecture

### Supervisor process model

```
python main.py
  │
  ├── uvicorn (FastAPI dashboard)          port 8080, 127.0.0.1 only
  │     └── SSE /events endpoint  ──────► browser auto-refresh
  │
  ├── asyncio.Task: ha_log_monitor_loop    SSH log tail → AI triage → HITL card
  ├── asyncio.Task: resource_poll_loop     disk/memory every resource_poll_interval_s
  ├── asyncio.Task: update_check_loop      update entity poll every update_check_interval_hours
  ├── asyncio.Task: notification_poll_loop persistent_notification.* every notification_poll_interval_s
  ├── asyncio.Task: netalertx_loop         health poll + log tail (if netalertx_host set)
  │
  └── EventBus (asyncio.Queue)             all tasks → SSE endpoint → browser
```

Each monitoring task is wrapped by the supervisor in an exception-catching restart loop with
exponential backoff (max 5 min). A crashed task emits a `loop_error` event to the bus and
restarts after the backoff delay. The supervisor catches `SIGTERM`/`SIGINT` and cancels all
tasks cleanly before exit.

### Config live-apply

`config.py` currently loads at module import time. For live apply to work without a restart,
the supervisor and all loop code must reference config values as `config.X` (module attribute
lookup) rather than `from config import X` (copied binding). Item 55 includes a one-time
audit of all loop call sites to enforce this pattern where it matters for runtime params.

Connection parameters (`HA_HOST`, `HA_USER`, `SSH_KEY_PATH`, `HA_API_TOKEN`) change the
SSH/REST connection and cannot be live-applied. Changes to these write to `config.yaml` and
show a restart prompt in the UI.

### Dashboard real-time updates (SSE)

A single `GET /events` endpoint streams Server-Sent Events to the browser. Each event is a
JSON payload:

| `event_type`   | Fields                                       | Triggers UI update                     |
|----------------|----------------------------------------------|----------------------------------------|
| `loop_status`  | `loop`, `status`, `last_run`, `next_run`, `error` | Loop health row in overview        |
| `hitl_card`    | `card_id`, `subject`, `severity`, `card_type` | Badge count; new card in queue tab    |
| `timeline`     | `ts`, `level`, `source`, `message`, `detail_url` | Prepend row to event timeline       |
| `resource`     | `disk_free_gb`, `disk_used_pct`, `mem_free_gb` | Resource gauges in overview         |
| `ha_state`     | `version`, `state`, `config_valid`            | HA status card in overview             |
| `config_saved` | `key`, `new_value`                            | Flash confirmation on settings tab     |

The browser reconnects automatically if the stream drops (standard SSE behaviour).

### HITL card dispatch

All HITL cards gain a `card_type` string field (set by the caller). The dashboard
`POST /approve/{nid}` dispatches to a typed handler:

| `card_type`           | Handler                     | Calls into                              |
|-----------------------|-----------------------------|-----------------------------------------|
| `repair`              | `_execute_queued_fix()`     | `ha_agent_sandbox_engine`               |
| `update`              | `_execute_queued_update()`  | `ha_update_manager.execute_update()`    |
| `netalertx_heal`      | `_execute_netalertx_heal()` | `netalertx.healer.run_heal()`           |
| `resource_action`     | `_execute_resource_action()`| `ha_agent_advanced` resource functions  |
| *(unknown / legacy)*  | touch `.approved` only      | —                                       |

Each handler writes `{nid}.in_progress` at start, then `.approved` or `.rejected` on
completion. The dashboard shows a spinner for in-progress cards and prevents double-submit.

---

## Deliverables

### Item 55 — Supervisor process: asyncio task launcher

`main.py` default (no `--mode`) starts supervisor mode. Each monitoring loop is wrapped as
a named asyncio task. A `LoopSupervisor` class tracks task health, publishes `loop_status`
events to the event bus, and restarts crashed tasks with exponential backoff (2s → 5min cap).

Config audit: replace `from config import X` with `import config; config.X` for all
runtime-tunable params in loop code (`log_confidence_threshold`,
`resource_poll_interval_s`, `update_check_interval_hours`,
`notification_poll_interval_s`, `autonomy_level`, `self_healing_enabled`).

A loop is disabled if its controlling config interval is 0 (`update_check_interval_hours: 0`
means the update check loop does not start). `netalertx_loop` is disabled if `netalertx_host`
is unset.

**Tests:** supervisor starts all enabled loops; disabled loop (interval=0) is skipped; crashed
task restarts after backoff; SIGTERM cancels cleanly.

---

### Item 56 — Card-type dispatch infrastructure

`utils/card_types.py` — string constants (`CARD_TYPE_REPAIR`, `CARD_TYPE_UPDATE`, etc.).

Update every HITL card creation call site to include `"card_type": CARD_TYPE_X` in the JSON
payload:
- `ha_update_manager.py` → `CARD_TYPE_UPDATE`
- `ha_agent_sandbox_engine.py` → `CARD_TYPE_REPAIR`
- `netalertx/healer.py` → `CARD_TYPE_NETALERTX_HEAL`
- `utils/resource.py` → `CARD_TYPE_RESOURCE_ACTION`

Replace `if pending_fix_yaml` branch in `approve()` with a dispatch table. Legacy cards
(no `card_type` but has `pending_fix_yaml`) still route to `_execute_queued_fix()`.

**Tests:** each card type routes to the correct handler; unknown type falls back to
`.approved`-only; legacy card (no `card_type`) routes correctly.

---

### Item 57 — Update executor + in-progress spinner

`_execute_queued_update(nid, data, json_path, watch_dir)` in `web/dashboard.py`:
1. Write `{nid}.in_progress`
2. Parse `component` and `latest_version` from payload
3. Call `ha_update_manager.execute_update(component, latest_version)` (refactor this into
   a standalone async function, currently embedded in the one-shot pipeline)
4. Success: write `fix_applied: true` to card JSON, touch `.approved`, remove `.in_progress`
5. Failure: write `fix_error: <msg>` to card JSON, touch `.rejected`, remove `.in_progress`

Dashboard template: cards with `.in_progress` file show a spinner row instead of
Approve/Reject buttons.

**Tests:** success path → `.approved` + `fix_applied`; failure path → `.rejected` +
`fix_error`; in-progress state shown while handler runs.

---

### Item 58 — NetAlertX + resource action executors

`_execute_netalertx_heal(nid, data, ...)`: parse `heal_action` + `target` from payload;
call `netalertx.healer.run_heal(heal_action, target)`. Same success/failure pattern.

`_execute_resource_action(nid, data, ...)`: parse `action` from payload
(`"offload_backups"`, `"enforce_retention"`); dispatch to the appropriate
`ha_agent_advanced` function. Backup invariant enforced inside these functions.

**Tests:** heal and resource paths route correctly; error caught and written to card JSON.

---

### Item 59 — Dashboard home: system status overview

Replace the current HITL-cards-only homepage (`/`) with an overview tab layout:

**Overview tab (default):**
- HA state card: version, update available badge, config check status, last backup time
- Resource gauges: disk free bar (red at critical), memory free
- Loop health table: one row per loop — name, status (running/paused/error), last run,
  next scheduled run, error count since last start
- Pending HITL count with link to Queue tab
- Recent events: last 10 timeline entries

**Queue tab:** current HITL cards (moved from `/`)

**SSE wiring:** `GET /events` endpoint reads from the `EventBus` asyncio queue and streams
to the browser. Overview tab subscribes with `EventSource('/events')` and updates loop rows,
gauges, and timeline in place without page reload.

**Tests:** SSE endpoint yields events; loop_status events update in-memory state correctly.

---

### Item 60 — Live event timeline + drill-down

All significant Pueo actions emit a `timeline` event to the event bus with a `detail_url`.

**Events emitted:**
- Log triage result (INFO / WARN / ERROR actionable) → `detail_url=/events/<id>`
- HITL card sent → `detail_url=/cards/<nid>`
- Repair executed (success/fail) → `detail_url=/repairs/<nid>`
- Disk alert → `detail_url=/resources`
- Update check result → `detail_url=/updates`

**Timeline tab in dashboard:** paginated list of all timeline events, filterable by level
and source. Click any row → detail view showing:
- Full evidence bundle (raw log snippet, YAML, SSH output)
- LLM prompt + response (collapsible, from `LLMTrace`)
- Tool call sequence (from `AgentLoopResult`)
- Action taken and outcome
- Backup slug used (if any), linked to backup inventory

Timeline events are persisted to a new `timeline_events` SQLite table (migration v6) so
history survives restarts.

**Tests:** each emitter calls the bus; timeline events written to DB; detail view renders
without errors for all event types.

---

### Item 61 — Configuration editor (settings tab)

**Settings tab** in dashboard: all editable config params, grouped:

| Group | Params |
|-------|--------|
| Autonomy | `autonomy_level` (1–4 slider), `self_healing_enabled`, `hitl_always` |
| Monitoring intervals | `resource_poll_interval_s`, `update_check_interval_hours`, `notification_poll_interval_s` |
| Thresholds | `log_confidence_threshold`, `ha_disk_warn_gb`, `ha_disk_critical_gb`, `max_repairs_per_hour` |
| Notifications | `notify_method`, `ntfy_topic`, `ntfy_server`, `webhook_url` |
| HA Connection *(restart required)* | `ha_host`, `ha_user`, `ha_api_port` |

Each param renders as an appropriate input (slider, toggle, number field, text field) with
its current value, valid range hint, and one-line description.

`POST /config` endpoint: validates the new value, writes to `config.yaml`, updates the
`config` module attribute in-memory (for runtime params), emits `config_saved` event to SSE
bus. Connection params write to yaml and respond with `restart_required: true` — the UI
shows a restart banner.

**Tests:** valid change → config.yaml updated + module attr updated; invalid value (out of
range) → 400 response; connection param → `restart_required` flag set; settings tab renders
all params.

---

### Item 62 — Loop control from dashboard

Each loop row in the overview has **Pause** / **Resume** and **Run Now** buttons.

- **Pause**: sets a `paused` flag on the `LoopSupervisor` entry; the loop's next iteration
  checks this flag and sleeps instead of running. Emits `loop_status` event with
  `status: "paused"`.
- **Resume**: clears the flag. Loop resumes on its normal schedule.
- **Run Now**: schedules the loop's next iteration immediately (sets `next_run = now`).

`POST /loops/{loop_name}/pause`, `/resume`, `/run-now` endpoints.

**Tests:** pause → loop skips next iteration; resume → loop runs; run-now → loop fires
within 2s.

---

### Item 63 — launchd service install

`deploy/pueo.launchd.plist.template`: a plist template with `{{ PUEO_DIR }}` and
`{{ PYTHON_PATH }}` placeholders. Runs `python main.py` (supervisor mode), captures stdout
and stderr to `pueo.log`.

`setup.sh` gains a final step: "Install Pueo as a launchd service?" — if yes, substitutes
paths, copies to `~/Library/LaunchAgents/com.pueo.agent.plist`, and runs
`launchctl load ...`.

Dashboard: **Service** section in the settings tab showing service status (loaded/running),
with **Install**, **Restart**, and **Uninstall** buttons that shell out to `launchctl`.

**Tests:** template substitution produces a valid plist; `--mode install-service` installs
and loads (integration test, macOS only).

---

### Item 64 — `--mode audit`: Pueo self-diagnostics

A one-shot command that produces a structured report of the gap between Pueo's intended
operational state and its actual state. Output saved to `audits/pueo-audit-<date>.md`.

**Checks run:**
- Which loops are currently running (via PID file or launchctl query)
- HA disk free vs. `ha_disk_critical_gb` threshold
- Backup registry: slugs present on HA vs. tracked in DB; any unknown slugs
- Pending HITL cards (cards with no `.approved`/`.rejected` file)
- NetAlertX: last scan age, device count, MQTT status
- `state_history`: ratio of `is_valid=0` entries; any `unknown_slug` rows
- Update check: last run time, any pending updates

Each check emits a `[OK]`, `[WARN]`, or `[CRITICAL]` status with a one-line explanation
and a recommended action. The report closes with a priority-ordered action list.

This automates the manual audit documented in `audits/ha-operational-state-2026-07-29.md`.

**Tests:** each checker returns a structured result; report written to audits/; priority
ordering (CRITICAL before WARN before OK).

---

## Execution order

55 → 56 → 57 → 58 → 59 → 60 → 61 → 62 → 63 → 64

Items 55 and 56 are the structural foundations; 57–58 complete the dispatch handlers;
59–62 rebuild the dashboard UI; 63–64 are operational polish.

---

## Validation gate

- `python main.py` starts all loops and serves the dashboard; loop health rows appear in the
  overview tab.
- Approving the pending HA Core update card executes `execute_update()` directly in the
  dashboard process.
- Changing `autonomy_level` in the settings tab updates `config.autonomy_level` immediately
  and is reflected in the next autonomy gate decision.
- Pausing the `ha_log_monitor` loop stops triage; resuming restarts it.
- A launchd plist is installed and Pueo restarts automatically after `killall python`.
- `--mode audit` produces a report identifying the disk CRITICAL condition and pending update.

---

## What this does not cover

- Cloud escalation (Phase 18 / items 65–68): the dispatch table gains a
  `CARD_TYPE_CLOUD_ESCALATION` handler when that phase lands.
- Multi-agent coordination: the supervisor runs all loops in one process. If independent
  process isolation becomes desirable (e.g., a crashing NetAlertX loop must not affect HA
  monitoring), revisit ADR 005 at that point.
- Dashboard auth: binds to `127.0.0.1` only. If remote access is needed (Tailscale, VPN),
  add a `dashboard_password` config key at that time.

---

## Related

- `audits/ha-operational-state-2026-07-29.md` — original gap analysis that motivated this
- `docs/decisions/002-safety-invariant.md` — backup-before-write enforced inside each handler
- `docs/decisions/005-asyncio-over-agentic-framework.md` — ADR for plain asyncio; revisit
  trigger noted there applies if the supervisor grows beyond single-process coordination
- `docs/plan/cloud-escalation.md` — Phase 18; adds a card_type handler to the dispatch table
