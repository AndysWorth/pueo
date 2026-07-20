# NetAlertX Integration — Items 10–19

Part of the [Implementation Plan](../implementation-plan.md) · Phase 4 · 11–14 sessions.

**Depends on Phase 3.5 (item 9.5 — `AutonomyGate` must be implemented before item 10).**

> **Version targets** (re-verify at session start — both release monthly):
> - NetAlertX **v26.7.1** (2026-07-01)
> - Home Assistant **2026.7.2** (2026-07-10)
>
> **Version-specific constraints:**
> - Use only current REST API endpoints — `/API_OLD` is being removed in the next NetAlertX release
> - Log path is `app.log` — `stdout.log` was removed in v26.7.1
> - Webhook payload fields are camelCase (`eveMac`, `eveIp`, `eveDateTime`, `eveEventType`, `devVendor`, `devComments`) — canonical since v26.4.6
> - Docker volume path is `/data` (not `/app`) — baseline since v25.11.29
> - HA MQTT integration must be UI-based only — `mqtt:` in `configuration.yaml` blocks auto-discovery on current HA

---

### 10. NetAlertX Foundation — Package, Config, and API Client ✅ Done (2026-07-19) — PR #18
**Depends on:** Item 9.5 (AutonomyGate, FakeAutonomyGate, hitl_timeout_minutes config key)

**Problem:** Before any NetAlertX installer or monitoring logic can be written, Pueo needs the package skeleton, all NetAlertX configuration keys registered in the triple (config.py / config.yaml.default / setup.sh), the database migration for install state, and a working API client. Every later NetAlertX item depends on these.

**All 14 `netalertx.*` config keys are registered here** — done once rather than spread across items 11–19:
- `netalertx.enabled` (default false)
- `netalertx.deployment` (`auto|addon|docker`, default `auto`)
- `netalertx.host` (default: same as `home_assistant.host`)
- `netalertx.api_port` (default 20212)
- `netalertx.api_token` (no default — required when `netalertx.enabled = true`)
- `netalertx.ssh_host`, `netalertx.ssh_user`, `netalertx.ssh_key_path` (default: same as HA SSH config)
- `netalertx.addon_repository_url` (default `https://github.com/jokob-sk/NetAlertX`)
- `netalertx.addon_slug` (default `""` — if blank, installer auto-resolves from Supervisor store and caches in `netalertx_install_state.details_json`; a non-blank value in config takes precedence and skips auto-resolution)
- `netalertx.scan_interface` (default `""` — if blank, installer auto-detects via `ip route show default`)
- `netalertx.auto_generated_name_patterns` (default `["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"]`)
- `netalertx.max_scan_age_minutes` (default 20)
- `netalertx.mqtt_subscribe` (default true)
- `netalertx.log_container_name` (default `netalertx`)
- `netalertx.max_db_history_rows` (default 100000)

**New SQLite migration** (`_migrate_vN()` in the existing migration runner): add `netalertx_install_state` table — `id INTEGER PRIMARY KEY`, `state TEXT`, `correlation_id TEXT`, `timestamp TEXT`, `details_json TEXT`.

**New files:**
- `netalertx/__init__.py` — empty package init.
- `netalertx/detector.py` — probe HA Supervisor add-on API and Docker over SSH; return `DeploymentInfo(mode: str, container_name: str, api_base_url: str, log_path: str, version: str)`; `mode` is `"addon"` or `"docker"`; `version` parsed from HTTP GET `<api_base_url>/api/v1/about`.
- `netalertx/api_client.py` — async HTTP client via `httpx`; Bearer token from `netalertx.api_token`; methods: `get_devices() -> list[dict]`, `get_events() -> list[dict]`, `get_metrics() -> dict`, `get_settings() -> dict`, `trigger_scan() -> None`, `get_about() -> dict`.

**Modify `main.py`:**
- Add `--mode netalertx-setup` dispatch → `netalertx/installer.py` `main()` (implemented in items 11–12); guard with `netalertx.enabled` check; also runs automatically at startup when `netalertx.enabled = true` and persisted state is not `FULLY_OPERATIONAL`.
- Add `--mode netalertx` dispatch → `netalertx/log_monitor.py` `main()` (implemented in item 15); stub acceptable here.

**New dependencies:** `httpx` → `requirements.txt`

**Done when:**
- `python -c "import netalertx"` succeeds.
- All 14 `netalertx.*` keys present in `config.py`, `config.yaml.default`, and `setup.sh`.
- SQLite migration applies cleanly against a real `ha_agent_state.db`.
- `TestNetAlertXDetector` passes — Supervisor path and Docker fallback — using `FakeSSHClient`.
- `TestNetAlertXAPIClient` passes all 6 methods using `httpx.MockTransport`.
- `main.py --mode netalertx-setup` and `--mode netalertx` accept without error (stubs OK).

---

### 11. NetAlertX Installer — Steps 1–4 ✅ Done (2026-07-19) — PR #19
**Depends on:** Items 9.5 (AutonomyGate, FakeAutonomyGate), 10 (config keys, SQLite migration, detector)

**Problem:** Before NetAlertX can be monitored or healed, it must be installed. This item creates the installer state machine scaffold and implements the first four steps: discovering the deployment target, installing the Mosquitto broker, detecting the scan network interface, and adding the add-on repository.

**New file: `netalertx/installer.py`** — idempotent install/configure pipeline.

Install states persisted to `netalertx_install_state` after each completed step; next run resumes from the highest completed state:
```
NOT_INSTALLED → MQTT_INSTALLED → MQTT_RUNNING → ADDON_REPO_ADDED
  → ADDON_INSTALLED → ADDON_RUNNING → NETALERTX_CONFIGURED
  → HA_MQTT_INTEGRATION_VERIFIED → HA_AUTOMATION_CREATED → FULLY_OPERATIONAL
```

**Invariants for all steps:** Entry and exit logged with a shared correlation ID. File writes call `execute_remote_backup()` first; backup failure aborts the step. On step failure: log structured error → `gate.require_approval(risk=CRITICAL, ...)` to notify; abort. Prior steps are not rolled back — each is idempotent on re-entry.

**Step 1 — Detect deployment target**
- SSH: `ha supervisor info`; success → `mode = "addon"`.
- Failure → SSH: `docker info`.
  - Docker found → `gate.require_approval(risk=MEDIUM, ...)`: "Supervisor unavailable, falling back to Docker — confirm?"; on approval → `mode = "docker"`.
  - Neither found → `gate.require_approval(risk=CRITICAL, ...)` + abort.
- Persist `mode` to `details_json`; advance to `MQTT_INSTALLED`.

**Step 2 — Install Mosquitto MQTT broker**
- SSH: `ha addons info core_mosquitto`; inspect `state`.
- Not installed → `gate.require_approval(risk=HIGH, ...)`: "Mosquitto not found; installing will affect all HA MQTT integrations — approve?"; on approval: `ha addons install core_mosquitto && ha addons start core_mosquitto`; on rejection: abort with log.
- Installed but not running → `ha addons start core_mosquitto` (LOW risk, auto-proceeds at level ≥ 3).
- Verify: poll `ha addons info core_mosquitto` until `state == running` (30 s timeout, `@async_retry`).
- Advance to `MQTT_RUNNING`.

**Step 3 — Auto-detect network scan interface**
- If `netalertx.scan_interface` non-blank → use it directly; log and skip detection; advance.
- SSH: `ip route show default`; parse `dev <interface>` from default route line.
- Exactly one candidate → persist to `details_json`; log.
- Multiple candidates → `gate.require_approval(risk=MEDIUM, ...)` listing each interface and its associated subnet; on approval use the confirmed interface; on rejection abort.
- Detection fails and config key blank → `gate.require_approval(risk=CRITICAL, ...)` with manual resolution instructions + abort.

**Step 4 — Add add-on repository and resolve add-on slug**
- SSH: `ha store repositories list`; if `netalertx.addon_repository_url` already present → log and skip the add.
- If absent → SSH: `ha store repositories add <netalertx.addon_repository_url>`; re-verify; abort + HITL if still absent.
- Resolve slug: SSH: `ha store addons` filtered by repository URL; parse slug. If `netalertx.addon_slug` is blank in config, persist resolved slug to `details_json`. A non-blank config value takes precedence and skips this lookup.
- Advance to `ADDON_REPO_ADDED`.

> **Note on slug persistence:** The resolved slug is stored in `netalertx_install_state.details_json`, not written back to `config.yaml`. This avoids mutating the config file at runtime (config.py loads at import time; runtime writes would not be reflected in the running process). Subsequent installer steps read the slug from `details_json`. Users may set `netalertx.addon_slug` in `config.yaml` to skip auto-resolution.

**Done when:**
- `python main.py --mode netalertx-setup` walks steps 1–4, pauses at approval gates, and leaves state at `ADDON_REPO_ADDED`.
- A second run is a complete no-op for steps 1–4.
- `TestNetAlertXInstallerSteps1to4` covers each step: happy path, each rejection path, and idempotency; uses `FakeSSHClient`, `FakeNotifier`, and `FakeAutonomyGate`.

---

### 12. NetAlertX Installer — Steps 5–8 ✅ Done (2026-07-19) — PR #20
**Depends on:** Item 11 (installer scaffold + steps 1–4, state at `ADDON_REPO_ADDED`)

**Problem:** The add-on repository is registered but NetAlertX is not yet installed, configured, or linked to HA. This item completes the installer, ending with a fully configured and verified system.

Extends `netalertx/installer.py` with steps 5–8.

**Step 5 — Install and start the NetAlertX add-on**
- Read slug from `netalertx_install_state.details_json` (or `netalertx.addon_slug` if non-blank in config).
- SSH: `ha addons install <slug>`; poll `ha addons info <slug>` until `state != "unknown"` (5 min timeout, `@async_retry` with exponential backoff).
- SSH: `ha addons start <slug>`; poll until `state == "running"`.
- Timeout → `gate.require_approval(risk=CRITICAL, ...)` + abort.
- Advance to `ADDON_RUNNING`.

**Step 6 — Configure `app.conf`** (risk=HIGH)
- SSH: `ha addons info <slug>` → `data_path` field.
- SFTP-read existing `app.conf` from `<data_path>/app.conf` (may be a default stub on first run).
- `execute_remote_backup()` before any write; record slug.
- Build merged config from template (not LLM — deterministic values only):
  - `MQTT_BROKER` — `netalertx.host` (defaults to same as HA host)
  - `MQTT_PORT` — 1883
  - `HA_URL` — `http://<netalertx.host>:8123`
  - `HA_BEARER_TOKEN` — token from `home_assistant` config (same token used by the MCP server)
  - `SCAN_SUBNETS` — interface + subnet from Step 3 `details_json`
  - `TIMEZONE` — SFTP-read from `/config/configuration.yaml` `homeassistant.time_zone`
  - `LOADED_PLUGINS` — merge existing value with `MQTT` and `ARPSCAN`; do not overwrite other plugins
- SFTP-write merged `app.conf`; log structured diff (original vs. new) with correlation ID.
- SSH: `ha addons restart <slug>`; poll until running.
- Verify: HTTP GET `http://<netalertx.host>:<netalertx.api_port>/api/v1/about`; confirm 200.
- Advance to `NETALERTX_CONFIGURED`.

**Step 7 — Verify HA MQTT integration** (cannot be automated — UI-only on current HA)
- HTTP GET `http://<home_assistant.host>:8123/api/config/config_entries` with Bearer token; search for `domain == "mqtt"`.
- Found → log and advance to `HA_MQTT_INTEGRATION_VERIFIED`.
- Not found → `gate.require_approval(risk=LOW, ...)` with step-by-step UI instructions: "Go to Settings → Devices & Services → Add Integration → MQTT → broker: `<host>`, port: 1883, no credentials. Then signal approval." Poll every 60 s until the entry appears or rejection received (max `agent.hitl_timeout_minutes`).
- Note: `mqtt:` cannot be added programmatically — adding that YAML key disables MQTT auto-discovery on current HA (see Phase 4 version constraints).

**Step 8 — Create HA webhook automation for NetAlertX events** (risk=HIGH)
- SFTP-read HA automations directory (`/config/automations.yaml` or files under `/config/automations/`).
- If a NetAlertX webhook automation already exists (trigger `platform: webhook` + identifier containing `netalertx`) → log and skip.
- If absent → generate automation YAML with camelCase payload fields: `eveMac`, `eveIp`, `eveDateTime`, `eveEventType`, `devVendor`, `devComments` (schema canonical since NetAlertX v26.4.6).
- Write via existing HA sandbox engine (sandbox → verify → atomic swap → `ha core reload`); sandbox engine handles backup automatically.
- Log resulting webhook URL (e.g. `http://<ha_host>:8123/api/webhook/netalertx_event`) to structured log as a reminder for NetAlertX `HA_WEBHOOK_URL`.
- Advance to `HA_AUTOMATION_CREATED` then `FULLY_OPERATIONAL`.

**Done when:**
- `python main.py --mode netalertx-setup` on a system at `ADDON_REPO_ADDED` completes all remaining steps and reaches `FULLY_OPERATIONAL`.
- A second run on a `FULLY_OPERATIONAL` system is a complete no-op.
- `TestNetAlertXInstallerSteps5to8` covers each step (happy path, timeout, rejection, idempotency) using `FakeSSHClient`, `FakeNotifier`, `FakeAutonomyGate`, and `httpx.MockTransport`.

---

### 13. NetAlertX Device Name Sync — Reading HA Names and Writing Safe Updates ✅ Done (2026-07-20) — PR #25
**Depends on:** Items 9.5 (AutonomyGate), 10 (API client, `netalertx.auto_generated_name_patterns`), 12 (system at `FULLY_OPERATIONAL` before first sync)

**Problem:** NetAlertX device names are MAC addresses and vendor strings. HA is the authoritative source of friendly names. This item reads all HA name sources, merges them by MAC, and applies unambiguous writes (blank names and already-matching names). Conflict and unknown-device cases are handled in item 14.

> **Assumption check (session start):** Before implementing, verify HA has at least one device with a MAC connection in the device registry. If zero MAC entries exist, `gate.require_approval(risk=LOW, ...)`: "HA device registry has no MAC→name mappings. Confirm to proceed with unnamed NetAlertX devices, or set up a network tracker integration in HA first." Do not abort silently.

**New file: `netalertx/ha_name_sync.py`** — `HaNameSync` class (injects `SSHClientProtocol` and `NetAlertXAPIClient`; no LLM needed).

`async def read_ha_names() -> dict[str, str]` — reads three sources in priority order; returns merged normalized MAC→name map; logs per-source counts:
- **Source 1 (highest):** SFTP-read `/config/.storage/core.device_registry`; parse `data.devices[]`; extract `["mac", ...]` connections; use `name_by_user` then `name`.
- **Source 2:** SFTP-check `/config/known_devices.yaml`; if present, parse `mac:` + `name:` entries. (Deprecated per HA roadmap but present in many installs.)
- **Source 3 (lowest):** HTTP GET `http://<home_assistant.host>:8123/api/states` with Bearer token; filter `device_tracker.*`; extract `attributes.mac_address` + `attributes.friendly_name`.
- Normalize all MACs to `AA:BB:CC:DD:EE:FF` (uppercase colon-delimited).

`async def sync_names() -> SyncReport` — full sync; this item implements Cases 1 and 2:
- Fetch NetAlertX devices via `api_client.get_devices()`; normalize `devMAC`.
- **Case 1** — `devName` blank or matches `netalertx.auto_generated_name_patterns` AND HA name exists: `POST /device/<mac>/update-column` (`columnName: "devName"`, `columnValue: <ha_name>`); then `POST /device/<mac>/field/lock` (`fieldName: "devName"`, `lock: true`); log `event: name_written`.
- **Case 2** — `devName` matches HA name (case-insensitive): lock (idempotent); log `event: name_already_correct`.
- Devices falling into Cases 3 and 4 are collected but not processed; they are returned in `SyncReport.conflicted` and `SyncReport.unnamed` for item 14 to handle.

**Pydantic schema:** `SyncReport(written: list[str], locked: list[str], conflicted: list[ConflictEntry], unnamed: list[UnnamedEntry], reverse_dns: list[str])`

**Sync trigger:** Full sync runs as a final sub-step of `netalertx-setup` (called from `netalertx/installer.py` after reaching `FULLY_OPERATIONAL`).

**Done when:**
- `netalertx-setup` on a HA system with named devices writes those names and locks `devName` in NetAlertX.
- `TestHaNameSyncReadSources` covers all three source readers and merge priority (Source 1 wins on conflict; Source 3 fills gaps not covered by 1 or 2).
- `TestHaNameSyncCases1And2` covers write+lock (Case 1) and idempotent-lock (Case 2); verifies call counts via `FakeSSHClient` and `httpx.MockTransport`.
- Assumption-check HITL fires when device registry has zero MAC entries.

---

### 14. NetAlertX Device Name Sync — Conflict Resolution and Unknown Devices ✅ Done (2026-07-20) — PR #26
**Depends on:** Item 13 (`HaNameSync` class, `SyncReport` schema, Cases 1–2 implemented)

**Problem:** Cases 3 (HA name conflicts with existing NetAlertX name) and 4 (no HA name at all) need separate handling — conflicts require user arbitration, unknowns need a multi-step fallback and a targeted per-device sync hook for the health monitor.

Extends `netalertx/ha_name_sync.py`:

**Case 3 — Name conflict** (`devName` non-empty and differs from HA name):
- Collect `ConflictEntry(mac, ha_name, netalertx_name)` per device.
- After all devices processed: single `gate.require_approval(risk=MEDIUM, ...)` listing all conflicts in a table (MAC | HA name | NetAlertX current name); wait for approval.
- On approval: write all HA names and lock each field. On rejection: log each as `event: conflict_skipped`; do not write.

**Case 4 — No HA name found:**
- **Step A** — existing plausible name: if `devName` non-empty and not matching `netalertx.auto_generated_name_patterns` → lock; log `event: hostname_plugin_name_kept`.
- **Step B** — reverse DNS: if Step A did not apply, SSH `host <devLastIP>`; if returned hostname is usable (not ending in `.in-addr.arpa`, not matching auto-generated patterns) → `POST /device/<mac>/update-column`; lock; log `event: reverse_dns_name_written`.
- **Step C** — still unnamed: collect `UnnamedEntry(mac, vendor, last_ip)`; after all devices, single `gate.require_approval(risk=LOW, ...)` listing all still-unnamed devices (MAC | Vendor | Last IP) with a note to name them in NetAlertX or in `/config/known_devices.yaml`.

`async def sync_device(mac: str) -> None` — targeted single-device sync; reads HA name sources, processes just this MAC through Cases 1–4. This method is called by `netalertx/health.py` (item 16) when a device with `devIsNew == true` or blank `devName` is detected.

**Done when:**
- A name mismatch triggers a HITL conflict notification; no write occurs until approved.
- A device with no HA name and a usable reverse-DNS hostname gets that hostname written and locked.
- A device with no HA name, no plugin name, and no DNS entry appears in the HITL unnamed list.
- `sync_device()` on a new MAC processes it through all Cases 1–4.
- `TestHaNameSyncCases3And4` covers: conflict detection; approval writes+locks; rejection skips; Step A/B/C fallback paths; `sync_device()` triggered by a new device.

---

### 15. NetAlertX Log Monitoring ✅ TODO
**Depends on:** Items 9.5 (AutonomyGate), 10 (config, `DeploymentInfo`, `netalertx.log_container_name`)

**Problem:** NetAlertX errors appear in `app.log` (the only log path since v26.7.1). Without a log monitor, Pueo has no real-time visibility into scan failures, MQTT disconnections, or plugin errors.

**New file: `netalertx/log_monitor.py`** — SSH tail of `app.log` (path from `DeploymentInfo.log_path`). Reuses the two-layer triage pattern from `ha_log_monitor.py`: fast regex pre-filter (`CRITICAL_LOG_PATTERN`) then Ollama `LogEvaluation` with `confidence_score > 0.7`. High-confidence findings pass through `gate.should_auto_execute(risk=HIGH)` before any dispatch to the healer (item 18); at levels 1–2 a notifier event is sent instead. Reconnects automatically on stream failure via `@async_retry`.

**Modify `main.py`:** Replace the `--mode netalertx` stub from item 10 with a real dispatch to `netalertx/log_monitor.py` `main()`.

**Done when:**
- Log lines matching `CRITICAL_LOG_PATTERN` are triaged by Ollama and classified as `LogEvaluation`.
- Stream failure triggers automatic reconnect (tested by injecting a mid-stream `ConnectionError` via `FakeSSHClient`).
- At `agent.autonomy_level = 1`, a high-confidence finding sends a notifier event but calls zero healer functions.
- `TestNetAlertXLogMonitor` passes with `FakeSSHClient`, `FakeLLMClient`, and `FakeAutonomyGate`.

---

### 16. NetAlertX Health Polling and MQTT ✅ TODO
**Depends on:** Items 10 (API client, all config keys), 14 (`HaNameSync.sync_device`), 15 (log monitor pattern established)

**Problem:** Log monitoring is reactive. Pueo also needs proactive visibility: scan freshness, device counts, and MQTT bridge health checked on a regular schedule.

**New files:**
- `netalertx/mqtt_subscriber.py` — `aiomqtt` async subscriber on `system-sensors/binary_sensor/+/state` and `system-sensors/sensor/+/state`; feeds device presence events into a shared `HealthReport`; reconnects gracefully on broker drop.
- `netalertx/health.py` — polls `api_client.get_devices()` every `netalertx.max_scan_age_minutes` minutes; consumes events from `mqtt_subscriber`; produces `HealthReport(last_scan_age_minutes: int, device_counts: dict, mqtt_active: bool, anomalies: list[str], netalertx_version: str)`. When a device has `devIsNew == true` or empty `devName`, calls `HaNameSync.sync_device(mac)`.

**New dependency:** `aiomqtt` → `requirements.txt`

**Done when:**
- `HealthReport` produced on every poll cycle with correct scan age and device counts.
- Scan older than `netalertx.max_scan_age_minutes` appears as an anomaly.
- MQTT presence event updates device state without a full API poll.
- New or blank-name device triggers `HaNameSync.sync_device(mac)` (verified via a `HaNameSync` fake injected in tests).
- Tests use `FakeSSHClient`, `FakeLLMClient`, `FakeAutonomyGate`, and a mock MQTT broker fixture.

---

### 17. NetAlertX AI Diagnosis ✅ TODO
**Depends on:** Items 10 (API client), 15 (`LogEvaluation` schema pattern), 16 (`HealthReport` schema)

**Problem:** Raw anomalies from the health monitor and log monitor need AI triage — not every issue warrants action, and the right fix depends on root cause (networking, MQTT, version change, HA config conflict).

**Build:**
- `prompts/diagnose_netalertx.md` — system prompt encoding v26.7.1 knowledge: API shape, known failure modes (ARP/host networking, MQTT `configuration.yaml` conflict, VLAN interface spec, iOS false-positives, `devFlapping`/`devIsSleeping` semantics, `app.log` path, plugin names).
- `prompts/triage_netalertx_log.md` — log-line triage prompt for NetAlertX-specific patterns.
- `NetAlertXDiagnostic` Pydantic schema — fields: `issue: str`, `severity: str`, `category: str` (`networking|mqtt|database|version|ha_integration`), `recommended_fix: str`, `affected_netalertx_version: str`.
- `netalertx/config_validator.py` — deterministic (non-LLM) checks returning `list[ConfigIssue(field, message, severity)]`:
  - `app.conf` required keys present and non-empty.
  - `LOADED_PLUGINS` contains `MQTT` and `ARPSCAN`.
  - HA `configuration.yaml` does not contain a top-level `mqtt:` key (blocks auto-discovery).
  - Webhook automation YAML field names are camelCase (required since v26.4.6).

**Done when:**
- A simulated "zero devices discovered" anomaly in a `HealthReport` produces a `NetAlertXDiagnostic` with `category=networking` and a recommended fix referencing `--network=host`.
- An `mqtt:` key in HA `configuration.yaml` is detected by `config_validator` and returned as a `ConfigIssue`.
- All tests use `FakeLLMClient`; no live Ollama call required.

---

### 18. NetAlertX Autonomy-Gated Healing ✅ TODO
**Depends on:** Items 9.5 (AutonomyGate, FakeAutonomyGate), 17 (`NetAlertXDiagnostic`, `config_validator`)

**Problem:** Diagnosis alone doesn't fix anything. Pueo needs to act on findings, with the level of autonomy controlled by `agent.autonomy_level` via `AutonomyGate`.

**New file: `netalertx/healer.py`** — action gating via `AutonomyGate`:
- **Level 1** (Report Only): log `NetAlertXDiagnostic` + send notifier event; zero file writes or restarts.
- **Level 2** (Suggest): propose each fix via `gate.require_approval(risk=..., ...)`; execute only on approval.
- **Level 3** (Guided): SFTP rewrite of `app.conf` via sandbox→verify→swap (MEDIUM risk — auto-proceeds); camelCase fix of HA automation YAML webhook fields (HIGH risk — requires approval via HA sandbox engine); removal of `mqtt:` key conflict from `configuration.yaml` (HIGH risk — requires approval via HA sandbox engine).
- **Level 4** (Autonomous): all level 3 actions without approval gates; additionally: `docker restart <container>` or add-on restart via HA Supervisor REST API (HIGH risk — auto-proceeds at level 4); API-triggered rescan after restart.

**Version change detection:** Persist last seen NetAlertX version in a new `netalertx_state` SQLite table (new `_migrate_vN()` migration: columns `id INTEGER PRIMARY KEY`, `key TEXT`, `value TEXT`). On version bump: run `config_validator` breaking-change check; at levels 1–3 `gate.require_approval(risk=HIGH, ...)` before any automated action; at level 4 log the bump and continue unless the breaking change is CRITICAL.

**Done when:**
- Level 1: config problem produces a notifier event and `FakeSSHClient.write_calls == []`.
- Level 3: `app.conf` rewrite proceeds automatically; `configuration.yaml` write pauses for approval.
- Level 4: repeated scan failure triggers a container restart + rescan without HITL.
- Version bump at level 3 triggers HITL before any action.
- All four levels tested with `FakeSSHClient`, `FakeNotifier`, and `FakeAutonomyGate`.

---

### 19. NetAlertX HA Integration Maintenance ✅ TODO
**Depends on:** Items 17 (`config_validator`, `ConfigIssue`), 18 (`healer`)

**Problem:** The link between NetAlertX and HA silently degrades — webhook automations drift from the current payload schema, MQTT entities stop registering, or DB tables grow until queries slow.

**Extend `netalertx/config_validator.py`:**
- Scan all HA automation YAML files for NetAlertX webhook automations; validate payload field names match the detected NetAlertX version schema (camelCase for v26.4.6+); return field-level `ConfigIssue` entries for any snake_case mismatches.
- Cross-reference NetAlertX API device list vs. HA MQTT entities (`device_tracker.*` with `attributes.mac_address`); collect divergence as `ConfigIssue(severity="WARNING")` for any MAC in NetAlertX not found as an MQTT entity.
- Poll API metrics for `Plugins_History` and `Events` row counts; return `ConfigIssue(severity="WARNING")` when above `netalertx.max_db_history_rows`.

**Extend `netalertx/healer.py`:**
- At level 3+: fix webhook automation field names to camelCase via HA sandbox engine (HIGH risk — requires approval at level 3; auto-proceeds at level 4).
- At level 4: trigger `DBCLNP` cleanup plugin via API when row count exceeds threshold.
- MQTT divergence anomalies send a notifier event at all levels; no auto-fix (requires investigating the HA integration, not a file change).

**Done when:**
- A webhook automation using old snake_case field names is detected and a `ConfigIssue` is returned.
- At level 3+, the automation is corrected via the sandbox engine (approval required at level 3).
- A device in the NetAlertX API but absent from HA MQTT entities produces a WARNING anomaly at all levels.
- A table exceeding `netalertx.max_db_history_rows` triggers a cleanup API call at level 4.
