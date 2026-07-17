# Agentic Engineering Practices — Implementation Plan

Items are ordered by effort × impact. Each phase builds on the previous.
Pick up any incomplete item at the start of a new session by reading this file.

---

## Phase 1 — Quick Wins (1 session each)

### 1. Prompt Management ✅ Done (2026-07-15)
**Problem:** System prompts are string literals scattered across four agent scripts. Changing a prompt requires finding and editing Python code.

**Build:**
- `prompts/diagnose_config.md` — system prompt for config analysis (used by `ha_agent_core.py`, `ha_agent_advanced.py`, `ha_agent_sandbox_engine.py`)
- `prompts/triage_log.md` — system prompt for log line triage (used by `ha_log_monitor.py`)
- `prompts/diagnose_config_repair.md` — variant used by sandbox engine that requests a full corrected YAML

**Modify:** Create `utils/prompts.py` with a `load_prompt(name: str, **kwargs) -> str` function that reads from `prompts/`, applies `str.format_map(kwargs)` for dynamic values, and caches the result.

**Modify agent scripts:** Replace all `system_prompt = "..."` string literals with `load_prompt("diagnose_config")`.

**Done when:** No system prompt strings exist in agent scripts; a prompt change requires only editing a `.md` file; prompts are tracked in git history separately from logic changes.

---

### 2. Retry with Exponential Backoff ✅ Done (2026-07-15)
**Problem:** SSH failures cause the log monitor to die with a flat 5-second retry. Transient Ollama timeouts raise immediately with no retry. Neither is production-safe.

**Build:** `utils/retry.py` — an `async_retry(max_attempts, base_delay, max_delay, exceptions)` decorator. Uses exponential backoff with ±25% jitter. Non-retryable exceptions (e.g., `ValidationError`, `PermissionError`) pass through immediately.

**Modify:**
- `ha_log_monitor.py` — replace recursive `tail_remote_log_stream()` retry with `@async_retry`
- `ha_agent_core.py`, `ha_agent_advanced.py`, `ha_agent_sandbox_engine.py` — wrap `asyncssh.connect()` calls with retry for `ConnectionError`, `TimeoutError`
- Ollama calls: retry on `ConnectionRefusedError` (Ollama not yet ready); do not retry on `ValidationError`

**Config keys to add:** `agent.ssh_retry_attempts` (default 3), `agent.ssh_retry_base_delay` (default 2.0)

**Done when:** A transient SSH drop during log monitoring recovers automatically; no flat `asyncio.sleep` retry loops exist in any agent script.

---

### 3. Rate Limiting and Debounce ✅ Done (2026-07-15)
**Problem:** A burst of HA errors triggers sequential repair attempts with no cooldown, potentially flooding HA with config writes and `ha core reload` calls.

**Build:** `utils/rate_limiter.py` — two primitives:
- `Debouncer(window_seconds)` — collects events over a time window, yields one aggregated trigger
- `RateLimiter(max_calls, period_seconds)` — token bucket; raises `RateLimitExceeded` if over budget

**Modify `ha_log_monitor.py`:**
- Wrap the error dispatch in a `Debouncer` — collect matching log lines for `debounce_window_seconds` before calling `trigger_remediation_pipeline()`
- Wrap `trigger_remediation_pipeline()` in a `RateLimiter` — enforce `max_repairs_per_hour`
- After a completed repair cycle, enforce a `repair_cooldown_seconds` sleep before re-arming

**Config keys to add:** `agent.debounce_window_seconds` (default 30), `agent.repair_cooldown_seconds` (default 300), `agent.max_repairs_per_hour` (default 10)

**Done when:** 50 simultaneous HA errors produce exactly one repair attempt; back-to-back errors within the debounce window are coalesced; the cooldown prevents re-trigger during HA reload.

---

### 4. SQLite Migration Strategy ✅ Done (2026-07-15)
**Problem:** `state_history` and `backup_registry` schemas are created with `CREATE TABLE IF NOT EXISTS` but never versioned. Adding a column silently fails on existing databases.

**Modify `ha_agent_advanced.py` and `ha_agent_sandbox_engine.py`:**
- Add a `schema_version` table with a single `version INTEGER` row
- Replace `init_local_database()` with a migration runner that applies numbered migration functions in sequence: `_migrate_v1()`, `_migrate_v2()`, etc.
- Each migration is idempotent — safe to re-run
- Current schema becomes migration v1

**Done when:** Adding a column to `state_history` is a single `_migrate_vN()` function; existing installed databases upgrade automatically at agent startup without data loss.

---

## Phase 2 — Observability (1–2 sessions)

### 5. Structured Logging + Correlation IDs ✅ Done (2026-07-15)
**Problem:** Agent decisions and outcomes are printed to stdout with no structure, no queryability, and no way to trace a complete repair event end-to-end.

**Build:** `utils/logging.py` — configures Python's `logging` module with a JSON formatter. Each log record includes: `timestamp`, `level`, `event`, `correlation_id`, `module`, and any event-specific fields.

**Modify all agent scripts:**
- Replace all `print()` calls with structured log calls: `log.info("backup_created", slug=slug, correlation_id=cid)`
- Generate a UUID correlation ID at the start of each repair cycle in `ha_log_monitor.trigger_remediation_pipeline()`
- Pass correlation ID through every function in the pipeline: fetch → analyze → backup → sandbox → swap
- Store `correlation_id` in the `state_history` SQLite table (add via migration v2)

**Config keys to add:** `agent.log_level` (default "INFO"), `agent.log_file` (default "pueo.log")

**Done when:** `grep correlation_id=<uuid> pueo.log` shows the complete lifecycle of one repair event; no bare `print()` calls remain in agent code.

---

### 6. Context Window / Token Management ✅ Done (2026-07-15)
**Problem:** Large `configuration.yaml` files or long tracebacks are sent to a 7B model with no size check, risking context overflow and hallucination (violates the 8,000-token evaluation matrix constraint).

**Build:** `utils/context.py`:
- `estimate_tokens(text: str) -> int` — character-based estimate (÷4), fast and dependency-free
- `truncate_to_budget(text: str, max_tokens: int, strategy: str) -> str` — strategies: `"tail"` (keep recent), `"head"` (keep start), `"smart"` (keep first + last N lines)
- `sliding_window_lines(lines: list[str], max_tokens: int) -> list[str]` — for log streams

**Modify agent scripts:**
- Before each `ollama.chat` call: estimate combined prompt + content size; truncate content if over budget
- In `ha_log_monitor.py`: apply sliding window to accumulated log lines before triage
- Log a warning when truncation occurs (with original vs. truncated token count)

**Config keys to add:** `agent.max_prompt_tokens` (default 7000)

**Done when:** No prompt sent to Ollama ever exceeds `max_prompt_tokens`; a 50KB `configuration.yaml` is handled gracefully; truncation events appear in structured logs.

---

## Phase 3 — Architecture (2–3 sessions)

### 7. Agent Output Content Validation ✅ Done (2026-07-15)
**Problem:** Pydantic validates JSON structure but not YAML content. The agent could return a `recommended_fix_yaml` that is structurally valid JSON but deletes critical HA config blocks or introduces credentials.

**Build:** `utils/yaml_validator.py`:
- `validate_proposed_fix(original_yaml: str, proposed_yaml: str) -> ValidationResult`
- Checks: proposed YAML parses without error; `homeassistant:` block is present; no top-level keys were removed that existed in the original; proposed YAML is not empty; structural diff is < 80% changed (suspiciously large rewrites are flagged)
- Returns `ValidationResult(is_safe: bool, reasons: list[str])`

**Modify `ha_agent_sandbox_engine.py`:** Call `validate_proposed_fix()` after inference, before `execute_remote_backup()`. If `not result.is_safe`, log the reasons and abort — do not proceed to backup.

**Done when:** A proposed fix that removes the `homeassistant:` block is rejected before any backup is triggered; rejection reasons appear in structured logs.

---

### 8. Dependency Injection / Protocol Interfaces ✅ Done (2026-07-15)
**Problem:** SSH and Ollama calls are hardwired into agent functions, making it impossible to test the repair pipeline without live infrastructure.

**Build:**
- `interfaces.py` — `SSHClientProtocol` and `LLMClientProtocol` using `typing.Protocol`
- `utils/ssh_client.py` — `AsyncSSHClient` wrapping current `asyncssh` calls; `FakeSSHClient` for tests with configurable responses
- `utils/ollama_client.py` — `OllamaClient` wrapping current `ollama.chat` calls; `FakeLLMClient` for tests

**Modify agent scripts:** Functions currently calling `asyncssh.connect()` and `ollama.chat` directly accept client instances as parameters with real implementations as defaults (no call-site changes required for production use).

**Modify `tests/conftest.py`:** Add fixtures `fake_ssh_client` and `fake_llm_client` that return pre-configured fakes.

**Done when:** The full `ha_agent_sandbox_engine.main()` pipeline runs in tests using fake clients; test coverage of the repair pipeline reaches > 80%; no production behaviour changes.

---

### 9. HITL Notification Infrastructure ✅ Done (2026-07-15)
**Problem:** The HITL gate (roadmap item) requires a notification channel to be usable. Without one, "pause for human approval" means "pause forever."

**Build:** `utils/notify.py`:
- `NotifierProtocol` with `async def send(subject: str, body: str, payload: dict) -> None`
- `FileNotifier` — writes a JSON file to a watch directory; agent polls for a `.approved` or `.rejected` sibling file
- `NtfyNotifier` — HTTP POST to ntfy.sh (free, self-hostable push notifications); no account required for basic use
- `WebhookNotifier` — generic HTTP POST for integration with Home Assistant automations or other systems

**Config keys to add:** `agent.notifier` (default "file"), `agent.notify_url`, `agent.notify_watch_dir` (default "hitl/")

**Modify `ha_agent_sandbox_engine.py`:** Add a `requires_hitl(report: DiagnosticsReport) -> bool` function (initially: returns True if `severity == "CRITICAL"` or issue text mentions "hacs" or "database"). If True, send notification and wait for approval before proceeding to backup.

**Done when:** A CRITICAL severity finding sends a push notification and waits; an approval file in `hitl/` unblocks the pipeline; a rejection file aborts it; the whole flow is testable with `FakeNotifier`.

---

## Phase 4 — NetAlertX Integration (5–7 sessions)

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

### 10. NetAlertX Foundation ✅ TODO
**Problem:** Pueo has no awareness of NetAlertX. Before monitoring or healing can happen, Pueo needs to detect how NetAlertX is deployed and establish an authenticated API connection.

**Build:**
- `netalertx/__init__.py` — package init
- `netalertx/detector.py` — probe HA Supervisor add-on API and Docker CLI over SSH; return `DeploymentInfo(mode, container_name, api_base_url, log_path, version)`; version parsed from `/about` endpoint
- `netalertx/api_client.py` — async HTTP client via `httpx`; Bearer token auth; methods: `get_devices()`, `get_events()`, `get_metrics()`, `get_settings()`, `trigger_scan()`, `get_about()`

**Config keys to add:** `netalertx.enabled` (default false), `netalertx.mode` (`diagnose|auto_fix|autonomous`, default `diagnose`), `netalertx.deployment` (`auto|addon|docker`, default `auto`), `netalertx.host` (default: same as HA host), `netalertx.api_port` (default 20212), `netalertx.api_token`, `netalertx.ssh_host`, `netalertx.ssh_user`, `netalertx.ssh_key_path`

**Modify `main.py`:** Add `--mode netalertx` dispatch to `netalertx_monitor.py`

**New dependencies:** `httpx` → `requirements.txt`

**Done when:** `python main.py --mode netalertx` connects to a running NetAlertX instance, auto-detects add-on vs. Docker deployment, fetches device list via API, and logs a structured health summary; `TestNetAlertXDetector` and `TestNetAlertXAPIClient` pass using `httpx.MockTransport`.

---

### 11. NetAlertX Monitoring ✅ TODO
**Problem:** With API access established, Pueo needs continuous visibility into scan health, device presence, and log errors — not just on-demand polling.

**Build:**
- `netalertx/log_monitor.py` — SSH tail of `app.log` (path from `DeploymentInfo`); same `CRITICAL_LOG_PATTERN` + Ollama `LogEvaluation` triage loop as `ha_log_monitor.py`; reconnects automatically on stream failure via `@async_retry`
- `netalertx/mqtt_subscriber.py` — `aiomqtt` async subscriber on `system-sensors/binary_sensor/+/state` and `system-sensors/sensor/+/state`; feeds device presence events into `HealthReport`; graceful reconnect
- `netalertx/health.py` — polls API every N minutes and consumes MQTT events; produces `HealthReport(last_scan_age_minutes, device_counts, mqtt_active, anomalies, netalertx_version)`

**Config keys to add:** `netalertx.max_scan_age_minutes` (default 20), `netalertx.mqtt_subscribe` (default true), `netalertx.log_container_name` (default `netalertx`)

**New dependencies:** `aiomqtt` → `requirements.txt`

**Done when:** `HealthReport` is produced on a regular poll cycle; a scan older than `max_scan_age_minutes` appears as an anomaly; MQTT presence events update device state in real time; tests use `FakeSSHClient`, `FakeLLMClient`, and a mock MQTT broker fixture.

---

### 12. NetAlertX AI Diagnosis ✅ TODO
**Problem:** Raw anomalies from the health monitor need to be triaged — not every issue warrants action, and the right fix depends on root cause (networking, MQTT, version change, HA config conflict, etc.).

**Build:**
- `prompts/diagnose_netalertx.md` — system prompt encoding v26.7.1 knowledge: API shape, known failure modes (ARP/host networking, MQTT `configuration.yaml` conflict, VLAN interface spec, iOS false-positives, `devFlapping`/`devIsSleeping` semantics, `app.log` path)
- `prompts/triage_netalertx_log.md` — log-line triage prompt
- `NetAlertXDiagnostic` Pydantic schema — fields: `issue`, `severity`, `category` (`networking|mqtt|database|version|ha_integration`), `recommended_fix`, `affected_netalertx_version`
- `netalertx/config_validator.py` — validate `app.conf` required keys; check `LOADED_PLUGINS` contains `MQTT` and `ARPSCAN`; scan HA `configuration.yaml` for `mqtt:` key conflict; validate webhook automation YAML field names are camelCase (required since v26.4.6)

**Done when:** A simulated "zero devices discovered" anomaly produces a `NetAlertXDiagnostic` with `category=networking` and a recommended fix referencing `--network=host`; an `mqtt:` key in HA `configuration.yaml` is flagged by `config_validator`; all tests use `FakeLLMClient`.

---

### 13. NetAlertX Mode-Gated Healing ✅ TODO
**Problem:** Diagnosis alone doesn't fix anything. Pueo needs to act on findings, with the level of autonomy controlled by `netalertx.mode`.

**Build:**
- `netalertx/healer.py` — three modes:
  - `diagnose`: log structured finding + send HITL notification via existing `NtfyNotifier`/`FileNotifier`
  - `auto_fix`: SFTP rewrite of `app.conf` (sandbox→verify→swap pattern matching `ha_agent_sandbox_engine.py`); fix HA automation YAML webhook field names to camelCase; remove `mqtt:` YAML conflict from `configuration.yaml` (triggers existing HA sandbox engine)
  - `autonomous`: all of `auto_fix`, plus `docker restart <container>` or add-on restart via HA Supervisor REST API; API-triggered rescan after restart
- Version change detection — persist last seen NetAlertX version in Pueo's SQLite via a new `_migrate_vN()` migration adding a `netalertx_state` table; on version bump, run breaking-change check and send HITL notification before any automated action

**Done when:** In `diagnose` mode a config problem produces a HITL notification and no file changes; in `auto_fix` mode `app.conf` is rewritten and verified before applying; in `autonomous` mode a repeated scan failure triggers a container restart followed by a rescan; all three modes are tested with `FakeSSHClient` and `FakeNotifier`.

---

### 14. NetAlertX HA Integration Maintenance ✅ TODO
**Problem:** The link between NetAlertX and HA can silently degrade — webhook automations drift from the current payload schema, MQTT entities stop registering, or DB tables grow until queries slow to a crawl.

**Build (extend `netalertx/config_validator.py` and `netalertx/healer.py`):**
- Scan all HA automation YAML files for NetAlertX webhook automations; validate payload field names match detected NetAlertX version schema (camelCase for v26.4.6+); report and fix mismatches in `auto_fix`/`autonomous` modes
- Cross-reference NetAlertX API device list vs. MQTT entities visible in HA; alert on divergence (devices in API not registered as MQTT entities)
- Poll API metrics for `Plugins_History` and `Events` row counts; alert when above `netalertx.max_db_history_rows`; in `autonomous` mode trigger `DBCLNP` cleanup plugin via API

**Config keys to add:** `netalertx.max_db_history_rows` (default 100000)

**Done when:** A webhook automation using old snake_case field names is detected and flagged; a device present in the API but absent from HA MQTT entities produces an anomaly; a table exceeding `max_db_history_rows` triggers a cleanup action in `autonomous` mode.

---

## Phase 5 — Agent Quality (3–4 sessions)

### 15. Evals with Synthetic HA Scenarios ✅ TODO
**Problem:** There is no way to know if a prompt change, model upgrade, or new feature makes the agent better or worse at its actual job. Unit tests verify code; evals verify agent intelligence.

**Build:**
- `evals/scenarios/` — directory of `.yaml` files, each defining: `name`, `input_config` or `input_log_line`, `expected_is_valid`, `expected_severity`, `expected_issue_keywords: list[str]`, `fix_must_parse: bool`
- Minimum 10 scenarios covering: malformed YAML, missing required key, deprecated integration format, valid config (true negative), CRITICAL traceback log line, INFO line (true negative), ambiguous WARNING
- `evals/run_evals.py` — loads each scenario, runs it through the real Ollama inference pipeline (requires Ollama running), scores results, prints a summary table, saves scores to `evals/baseline.json` on first run, compares against baseline on subsequent runs
- Scoring metrics: `is_valid` accuracy, severity accuracy, issue keyword recall, fix YAML parse success rate, mean inference latency

**Add slash command:** `/project:run-evals` — runs `python evals/run_evals.py` and summarises results.

**Add to CI (optional):** A separate workflow job that runs evals against Ollama if available, gated so it doesn't block PR merges.

**Done when:** Running `python evals/run_evals.py` produces a score table against ≥ 10 scenarios; a deliberate prompt regression visibly drops the score; baseline is committed and tracked in git.

---

## Tracking

Update the status markers above (`✅ TODO` → `✅ Done`) as items are completed. Add the completion date and the PR/commit reference as a note.
