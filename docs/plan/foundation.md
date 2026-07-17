# Foundation, Observability, and Architecture — Items 1–9

All items in this file are **complete**. Preserved for historical context and architectural reference.

Part of the [Implementation Plan](../implementation-plan.md) · Phases 1–3.

---

## Phase 1 — Quick Wins

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

**Config keys added:** `agent.ssh_retry_attempts` (default 3), `agent.ssh_retry_base_delay` (default 2.0)

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

**Config keys added:** `agent.debounce_window_seconds` (default 30), `agent.repair_cooldown_seconds` (default 300), `agent.max_repairs_per_hour` (default 10)

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

## Phase 2 — Observability

### 5. Structured Logging + Correlation IDs ✅ Done (2026-07-15)
**Problem:** Agent decisions and outcomes are printed to stdout with no structure, no queryability, and no way to trace a complete repair event end-to-end.

**Build:** `utils/logging.py` — configures Python's `logging` module with a JSON formatter. Each log record includes: `timestamp`, `level`, `event`, `correlation_id`, `module`, and any event-specific fields.

**Modify all agent scripts:**
- Replace all `print()` calls with structured log calls: `log.info("backup_created", slug=slug, correlation_id=cid)`
- Generate a UUID correlation ID at the start of each repair cycle in `ha_log_monitor.trigger_remediation_pipeline()`
- Pass correlation ID through every function in the pipeline: fetch → analyze → backup → sandbox → swap
- Store `correlation_id` in the `state_history` SQLite table (add via migration v2)

**Config keys added:** `agent.log_level` (default "INFO"), `agent.log_file` (default "pueo.log")

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

**Config keys added:** `agent.max_prompt_tokens` (default 7000)

**Done when:** No prompt sent to Ollama ever exceeds `max_prompt_tokens`; a 50KB `configuration.yaml` is handled gracefully; truncation events appear in structured logs.

---

## Phase 3 — Architecture

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
**Problem:** The HITL gate requires a notification channel to be usable. Without one, "pause for human approval" means "pause forever."

**Build:** `utils/notify.py`:
- `NotifierProtocol` with `async def send(subject: str, body: str, payload: dict) -> None`
- `FileNotifier` — writes a JSON file to a watch directory; agent polls for a `.approved` or `.rejected` sibling file
- `NtfyNotifier` — HTTP POST to ntfy.sh (free, self-hostable push notifications); no account required for basic use
- `WebhookNotifier` — generic HTTP POST for integration with Home Assistant automations or other systems

**Config keys added:** `agent.notifier` (default "file"), `agent.notify_url`, `agent.notify_watch_dir` (default "hitl/")

**Modify `ha_agent_sandbox_engine.py`:** Add a `requires_hitl(report: DiagnosticsReport) -> bool` function (initially: returns True if `severity == "CRITICAL"` or issue text mentions "hacs" or "database"). If True, send notification and wait for approval before proceeding to backup.

**Done when:** A CRITICAL severity finding sends a push notification and waits; an approval file in `hitl/` unblocks the pipeline; a rejection file aborts it; the whole flow is testable with `FakeNotifier`.
