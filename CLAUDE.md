# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Pueo** is a local, privacy-first agentic AI system that monitors and self-heals a Home Assistant (HA) instance. It runs on macOS Apple Silicon. The LLM inference engine is configurable (`LLM_PROVIDER`): local Ollama only (default, no WAN for inference), Anthropic Claude API, or both (Ollama for autonomous cycles + Claude available for approved escalation). All HA communication goes over SSH/SFTP. Transparency is a first-class design goal: users should be able to see what Pueo has done (event timeline, repair episodes) and what it is currently thinking (live tool-call trace in Chat).

Source code lives in `pueo/`.

## Project Type: Team/Library

This is a multi-person project. The following procedure variations from `~/.claude/CLAUDE.md` are active:

- **Code review:** At least one approval required before merging; the author cannot merge their own PR.
- **Dependency changes:** Changes to `requirements*.txt` warrant a second reviewer; call out transitive dependency risk in the PR description.
- **Breaking changes:** Add a deprecation warning for at least one version, bump the semver major version, and include a migration guide in the changelog.
- **Branch lifespan:** Rebase onto `main` daily for branches open more than one day.
- **Branch strategy:** Feature branches off `main` (no `develop` branch); hotfix branches off the relevant release tag for production bugs.
- **Merge strategy:** Squash merge to keep `main` history clean.
- **Migrations:** Test against a real local copy of `ha_agent_state.db` and flag migrations explicitly in the PR; no staging environment exists.
- **Rollback:** Document the rollback plan (revert commit + migration version) in the PR description for migrations and production config writes.

## Commands

Run from the `pueo/` directory:

```bash
pip install -r requirements-dev.txt  # includes runtime deps + dev/test tooling

# Primary entry point (macOS)
pueo                          # start supervisor (all loops + dashboard)
python main.py                # same, without the background/PID wrapper
pueo-py                       # pyproject entry point (main:main), no wrapper

# Docker equivalents
docker compose up -d           # start supervisor in Docker
docker compose logs -f pueo    # follow live log
docker exec pueo-agent python main.py --mode rag-refresh   # RAG refresh in Docker

# Individual agents (for debugging; live under agents/)
python agents/ha_agent_core.py            # Read-only: SSH fetch + Ollama diagnosis
python agents/ha_agent_advanced.py        # + SQLite memory + backup triggering
python agents/ha_agent_sandbox_engine.py  # Full: sandbox-test-then-atomic-swap repair
python agents/ha_log_monitor.py           # Continuous: live SSH log tail + AI triage

# Tests
pytest
pytest tests/test_config.py::TestConfigDefaults::test_loads_values_from_yaml  # single test example

# Code quality (CI enforces all of these)
black --check .
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
mypy --ignore-missing-imports .
bandit -r . -x ./tests,./.venv
```

`setup.sh` installs `pueo` as a bash script in `$PATH` (venv activation, PID wrapper). The `pyproject.toml` entry point is `pueo-py` (runs `main:main` directly, no wrapper). Use `pueo` for normal operation; use `python main.py` or `pueo-py` for direct debugging.

## Architecture

### Primary runtime architecture

- **`main.py`** — single entry point with 24 `--mode` values. Default: `supervisor`. All agent module imports are deferred inside mode branches (not top-level) so `PUEO_CONFIG` is set before any module imports `config.py`.
- **`LoopSupervisor`** (`utils/agent/supervisor.py`) — manages all background tasks with exception catching and exponential-backoff restart (2s → 5-min cap). The dashboard ASGI app runs alongside all supervisor tasks.
- **`AgentLoop`** (`utils/agent/agent_loop.py`) — the universal reasoning engine. Used by all 7 agent pipelines and by the Chat endpoint. All significant Pueo decisions go through an `AgentLoop` session.

For a compact orientation map (file responsibilities, utils directory, interfaces, quick commands), see `docs/for-agents.md`.

### Agent files (`agents/`)

| Agent | Responsibility | Supervisor task(s) | `--mode` |
|---|---|---|---|
| `ha_agent_core.py` | Config fetch + LLM diagnosis (read-only) | — | `diagnose` |
| `ha_agent_advanced.py` | Diagnosis + SQLite memory (`ha_agent_state.db`) + backup triggering | — | `advanced` |
| `ha_agent_sandbox_engine.py` | Full repair: sandbox-test-then-atomic-swap | repair pipeline (triggered by log monitor) | `repair` |
| `ha_log_monitor.py` | Live SSH log tail with AI triage; also houses `poll_for_updates`, `poll_for_notifications`, `poll_for_repairs` | `ha_log_monitor`, `ha_log_monitor_supervisor`, `repair_poll`, `notification_poll`, `update_check` | `monitor` |
| `ha_lovelace_monitor.py` | Dashboard entity health + benign suppression | `lovelace_poll` | — |
| `ha_notification_manager.py` | Persistent notification triage and IP enrichment | `notification_poll` (for processing) | `notifications` |
| `ha_update_manager.py` | Update detection, breaking-change analysis, orchestration | `update_check` | `update-check` |

Agent scripts in `agents/` are runnable directly for debugging; under `supervisor` mode, all 7 run as coordinated background tasks managed by `LoopSupervisor`.

**Log monitor detail:** runs `ha core logs --follow` over SSH to stream live HA logs from the supervisor journal (modern HA does not reliably write to `/config/home-assistant.log`). Two-layer triage: fast regex pre-filter (`CRITICAL_LOG_PATTERN`) then Ollama `LogEvaluation` with `confidence_score > 0.7` threshold. High-confidence actionable errors trigger the repair pipeline. Reconnects automatically on stream failure.

**Repair pipeline (`ha_agent_sandbox_engine.py`):** When Ollama returns `is_valid=False` with a `recommended_fix_yaml`:
1. Run `validate_proposed_fix()` — abort if the proposed YAML removes critical keys or is suspiciously large
2. `AutonomyGate.require_approval()` — if CRITICAL severity or current autonomy level requires human approval, notify and wait
3. Trigger HA backup (mandatory)
4. Write proposed fix to `/config/.agent_sandbox/configuration.yaml` over SFTP
5. Temporarily swap it into `/config/configuration.yaml`, run `ha core check`, immediately revert (always, via `finally`)
6. Only if the sandbox check passes: atomically write to production and call `ha core restart`

## Key Patterns

**Structured LLM output**: All Ollama calls use `format=PydanticModel.model_json_schema()` and `temperature=0.0` to force deterministic, parseable JSON. Always wrap in `asyncio.to_thread()` since `ollama.chat` is synchronous.

**Safety invariant**: No write operation proceeds without a confirmed HA backup slug. Ordering is always `execute_remote_backup()` → `record_backup_slug()` → remediation. Never bypass this chain.

**Long-lived clients, bounded FDs**: LLM and embedding clients are created once and injected; never construct one per call or per log line (each opens an HTTP pool). `main.py::_raise_fd_limit()` lifts the soft `RLIMIT_NOFILE` to 4096 at startup (the launchd plist sets `NumberOfFiles` too), both uvicorn servers set `limit_concurrency`, and `resource_poll` logs `open_fds`/`fd_limit` plus `fd_warn` above 80% — checked before SSH so it still reports when FD exhaustion is why SSH fails (#769).

**SSH connections**: Each function opens its own `asyncssh.connect()` context. `known_hosts=None` is intentional for local-network HA hosts — flag in any security review.

**Single config source**: `config.py` is the only place settings are defined. Agent scripts must import from it (`from config import ...`) and must never redeclare constants. Adding a new setting means adding it to `config.yaml.default`, `config.py`, and `setup.sh` — nowhere else. `setup.sh` also generates `docker-compose.yml` for Docker deployments — changes to the compose file structure belong in `setup.sh`'s heredoc, not in `docker-compose.yml.example` (the committed placeholder template); `docker-compose.yml` is gitignored because setup.sh generates it with the user's SSH key path embedded.

**Config path resolution**: `config.py` loads at module import time. It checks the `PUEO_CONFIG` environment variable first, then falls back to `config.yaml` next to the script. `main.py` sets `PUEO_CONFIG` before importing any agent module so the right config file is used. Agent imports inside `main.py` must stay deferred (inside the `if args.mode` blocks) — moving them to the top of the file would break this.

**Platform directory abstraction**: `paths.py` at the repo root provides `PueoDirectories` (frozen dataclass) and `get_dirs()` factory. `main.py` calls `get_dirs().create_all()` early in startup. All mutable paths (DB, logs, HITL, backups, ChromaDB, tools, caches) derive from `get_dirs()` — never from `config.py` defaults alone. Override any directory with `PUEO_DATA_DIR`, `PUEO_STATE_DIR`, `PUEO_CACHE_DIR`, `PUEO_LOG_DIR`, `PUEO_CONFIG_DIR` env vars; the `Dockerfile` sets these to `/data`, `/state`, etc. for Docker. `resources_dir` (prompts, web assets, deploy templates) is always `Path(__file__).parent` of `paths.py` — immutable, not overridable. Tests use the `pueo_dirs` fixture (in `tests/conftest.py`) which sets `PUEO_*` env vars to `tmp_path` subdirectories so no test writes to `~/Library/`.

**Prompt loading via importlib.resources**: `prompts/` is a proper Python package (`__init__.py` present, declared in `pyproject.toml`). `utils/core/prompts.py::load_prompt()` uses `importlib.resources.files("prompts")` — works correctly in editable installs, wheels, and macOS `.app` bundles. `web/templates/` and `web/static/` still use `paths.get_dirs().resources_dir` because FastAPI's `StaticFiles`/`Jinja2Templates` require `Path` objects, not `Traversable`.

**Sandbox path derivation**: `SANDBOX_REMOTE_DIR` and `SANDBOX_REMOTE_FILE` in `agents/ha_agent_sandbox_engine.py` are derived from `CONFIG_REMOTE_PATH`, not independently hardcoded, so changing the config path in `config.yaml` automatically keeps the sandbox path in sync.

**Autonomy gate**: `AutonomyGate` in `utils/agent/autonomy.py` is the single approval decision point imported by all Pueo modules. Every action that touches remote state must call `gate.require_approval()` or `gate.should_auto_execute()` — no module may hard-code its own ask/skip logic. `FakeAutonomyGate` is the test double.

**Rate limiter / debouncer**: `Debouncer` and `RateLimiter` in `utils/core/rate_limiter.py` govern repair frequency. `DEBOUNCE_WINDOW_SECONDS` collapses rapid identical triggers; `MAX_REPAIRS_PER_HOUR` caps total actions in a rolling window. Both are enforced before any repair pipeline call.

**Stuck-loop backoff**: Every `_run_*_investigation` caller writes a 30-minute deferred suppression row (`mark_investigation_backoff` in `utils/hitl/hitl_tracker.py`) for any `outcome != "success"`. This prevents poll loops from immediately re-triggering the same session after a timeout or budget exhaustion. The backoff key format is `<session_type>_backoff:<entity_or_issue_key>`. The lovelace benign gate uses `should_send_card` (not a raw SQL check) so it respects both permanent benign suppression and the time-limited stuck backoff.

**Token budget management**: `estimate_tokens()` and `truncate_to_budget()` in `utils/core/context.py` enforce the 8,000-token evaluation matrix constraint. Every Ollama call site must trim content to `MAX_PROMPT_TOKENS` before dispatch — never pass unbounded YAML or log content.

**Dependency injection via Protocol interfaces**: `interfaces.py` defines `SSHClientProtocol`, `LLMClientProtocol`, `HARestClientProtocol`, `HAWebSocketClientProtocol`, and `HAEventSubscriberProtocol`. Agent functions accept these optional injected clients, falling back to real implementations when `None`. Tests pass `FakeSSHClient` / `FakeLLMClient` / `FakeHARestClient` / `FakeHAWebSocketClient` / `FakeHAEventSubscriber`; SSH, Ollama, and HA APIs are never called in the unit suite.

**Plain-text console formatter**: `_TextFormatter` in `utils/core/logging.py` is used on `stderr` when `setup_logging(console_text=True)` is called. The file handler always stays JSON. `main.py` enables `console_text` for `--mode netalertx-setup` to produce human-readable installer output.

**LLM-guided all actions — 6-phase methodology**: Every significant Pueo action flows through LLM tool-calling reasoning via `AgentLoop`. Infrastructure operations that bypass the LLM (scheduled scraper runs, disk-space enforcement, backup retention sweeps) are housekeeping, not decisions. The boundary rule: if a function changes HA state or makes a judgment call about what to do next, it belongs in an agent loop, not a direct call.

All agent sessions follow the **6-phase investigation cycle** (encoded in `prompts/agent_loop.md`; see ADR 018):
1. **Retrieve plan** — call `query_knowledge` first with the question or trigger. The result may include both background context and an investigation plan (runbook). If a runbook is returned, follow it as the starting point. If nothing relevant is returned, record this as a knowledge gap and proceed with first-principles reasoning.
2. **Form a hypothesis** — one sentence before calling any tool
3. **Gather evidence** — `read_config`, `read_logs`, `read_file`, `run_ha_command`, `read_pueo_log`, `fetch_ha_docs`. Call `query_knowledge` again if initial evidence narrows the problem to a specific sub-domain, confidence is low, or the agent is about to try a novel approach.
4. **Confirm root cause** — state it explicitly before acting; call `remember(key="<domain>_quirk", content="...")` for any non-obvious instance-specific fact (device quirk, known-wrong entity state, user preference) worth preserving across sessions
5. **Act** — apply fix, recommend action, or call `save_runbook` to record a novel approach
6. **Report** — call the terminal tool (`finish_repair`, `finish_chat`, `finish_investigation`)

**Stopping condition**: the agent does not stop because it hit an arbitrary tool count. It stops only when it has genuinely exhausted all reasonable investigative paths. Before calling the terminal tool with `outcome=failed`, the agent must call `save_runbook(type="gap")` documenting what was tried and why it is stuck, then call `request_escalation(reason)` so the user can route to a stronger model.

**Hard constraints — no concurrent LLM, no concurrent HA**: During supervised operation (when `PueoWorkQueue` is initialized), two rules are absolute: (1) Only one LLM call (`AgentLoop.run()` or any judgment-call LLM use) may run at a time. (2) Only one operation that writes to or restarts Home Assistant (SSH write, REST write, `ha backup new`, `ha core restart`, `ha os update`) may run at a time. Both constraints are enforced by routing through `PueoWorkQueue` — use `get_work_queue_or_none()` + `WorkItem`, fall back to direct `await`/`asyncio.create_task` only in test/standalone mode when the queue is not initialized. This applies to all callers: background monitor loops, the chat endpoint, and card handlers in `web/dashboard.py` (`approve()`, `apply_fixes()`). Card handlers use `priority=PRIORITY_HIGH`, `activity_type="card_execution"`, `dedup_key=f"card_{nid}"` — the dedup key prevents double-approval. One-shot LLM calls exempt from the queue: volume-throttled streaming pre-filters (`analyze_log_line_with_ai`) and secondary enrichment inside an already-running tool executor (`_enrich_fix_context`). Use `AgentLoop` for any LLM interaction that makes a judgment call, touches HA state, or could benefit from iterative evidence gathering. See ADR 018, ADR 025, ADR 026.

**Runbook library**: The `strategies` ChromaDB collection stores investigation approaches as runbooks. `save_runbook` (registered in all agent registries except code-proposal) embeds a runbook into ChromaDB and records it in the `agent_strategies` SQLite table. Three runbook states: **seed** (human-curated, embedded at RAG refresh via `strategy_seeder.py`), **candidate** (LLM-proposed during a successful session via `save_runbook(type="candidate")`, awaiting human review in the dashboard), and **gap** (failed session or empty KB result, documents what was tried and why stuck — always created on failure). Any session that gets nothing relevant from `query_knowledge` or ends with `outcome=failed` MUST save a gap runbook. `seed_home_profile()` in `strategy_seeder.py` generates a dynamic `ha_instance_profile` document (versions, installed integrations, HACS components, config keys) and upserts it into `strategies` at each RAG refresh (step 5.5). `prompts/seed_supervisor_cli.md` provides a Supervisor CLI reference (covering `ha apps`, `ha core`, `ha os`, `ha backups`, `ha supervisor`, `ha network`) so agents know which `run_ha_command` calls to use. See ADR 018.

**Developer docs**: `ha_developer_docs` is a ChromaDB collection (in `COLLECTIONS` in `knowledge_store.py`) that stores curated pages from `developers.home-assistant.io` (architecture, entity model, config flows, Supervisor API, WebSocket API, REST API). Populated by `utils/knowledge/ha_developer_docs_scraper.py` at RAG refresh (step 3.7). Authority score 1.0 — treated as official docs. Cache lives at `HA_DEVELOPER_DOCS_CACHE_DIR` (default `~/Library/Caches/Pueo/ha_developer_docs/`).

**Repair history**: `repair_history` is a ChromaDB collection (in `COLLECTIONS` in `knowledge_store.py`) that stores completed repair episodes as searchable text chunks. `embed_repair_episodes()` in `utils/knowledge/repair_episode_embedder.py` reads rows where `embedded_at IS NULL` from `repair_episodes` SQLite, embeds them via `format_episode_for_embedding()`, and marks `embedded_at`. Called as step 7 of `run_rag_refresh` in `main.py`. `AgentLoop._pre_inject_knowledge()` queries all `COLLECTIONS` including `repair_history`, so similar past repairs automatically appear as context before the first LLM call. A `pre_inject_similar_episodes` log entry is emitted when repair history chunks are injected. See ADR 031.

**HA best practices**: `ha_best_practices` is a ChromaDB collection (in `COLLECTIONS` in `knowledge_store.py`) that stores reference files from the [homeassistant-ai/skills](https://github.com/homeassistant-ai/skills) repo: deprecated API tables, YAML guidelines, dashboard patterns, template guidelines, and domain-specific docs. Populated by `utils/knowledge/ha_skills_scraper.py` at RAG refresh (step 8); files are refetched when the local cache is older than `RAG_REFRESH_INTERVAL_HOURS`. Authority score 0.9 — just below official HA developer docs. `_query_knowledge` labels these chunks `[BEST PRACTICE]`. Cache lives at `HA_SKILLS_CACHE_DIR` (default `~/Library/Caches/Pueo/ha_skills/`). All three `query_type` routes (`"diagnostic"`, `"procedural"`, `"generative"`) include this collection. See ADR 034.

**Authority-ranked knowledge retrieval**: `KnowledgeChunk` carries an `authority_score: float` set at query time by `_authority_score(collection, metadata)`. Scores range from 1.0 (official HA docs) to 0.30 (unreviewed candidate runbooks); see ADR 030 for the full tier table. `ChromaKnowledgeStore.query()` uses **hybrid BM25+cosine retrieval** (see ADR 029): each chunk's `score` is `cosine_sim × (1 − RAG_HYBRID_WEIGHT) + bm25_sim × RAG_HYBRID_WEIGHT` (default `RAG_HYBRID_WEIGHT=0.3`, so 70% cosine + 30% BM25). BM25 catches exact YAML key and service-name matches that cosine misses. The final ranking sorts by `(authority_score × 0.3) + (score × 0.7)` so semantics dominate but official sources edge out speculation at similar relevance. `_query_knowledge` in `tool_executor.py` prepends a text label (`[OFFICIAL]`, `[SEED RUNBOOK]`, `[CANDIDATE RUNBOOK – unreviewed]`, `[COMMUNITY RUNBOOK]`, `[PAST REPAIR]`, `[COMMUNITY]`, `[BEST PRACTICE]`) to each chunk so the model can reason about source trust. Knowledge chunks also carry optional `ha_version_min`/`ha_version_max` metadata set by scrapers at refresh time. `query_knowledge` applies **version-aware score boosting** when an HA version is known: chunks whose version range includes the running HA version get a 1.2× score multiplier; chunks older than 12 months get a 0.5× penalty. Pass `ha_version` explicitly (e.g. `"2026.9.0"`) or omit it for auto-detection from `self._ha_profile.ha_version`. See ADR 030. `query_knowledge` accepts an optional `query_type` (`"diagnostic"`, `"procedural"`, `"generative"`, `"version_check"`) to route to the most relevant collection subset; omit to search all collections (default). See `ToolExecutor._QUERY_TYPE_COLLECTIONS` in `utils/agent/tool_executor.py`.

**Agent self-awareness**: `read_source` is registered in all agent registries (`build_ha_tool_registry`, `build_netalertx_tool_registry`, `build_chat_tool_registry`, `build_code_proposal_registry`) in `utils/agent/tool_registry.py`. The LLM can call `read_source("utils/agent/tool_registry.py")` during any session to inspect which tools are available. Safety-critical paths (`utils/agent/autonomy.py`, `interfaces.py`, `config.py`) remain write-blocked by `_SAFETY_CRITICAL_PATHS` in `propose_patch` but are readable. See ADR 010.

**HA live lookup**: `fetch_ha_docs(domain, filename)` in `utils/agent/tool_executor.py` fetches HA component source or docs from GitHub raw (`homeassistant/core/dev/homeassistant/components/{domain}/{filename}`). In `local` mode it serves from cache only — a cache miss raises `ToolError` and makes no network call. In `cloud`/`both` mode it fetches live and writes to cache. Cache lives at `HA_SOURCE_CACHE_DIR` (default `~/Library/Caches/Pueo/ha_source/`). The RAG refresh cycle pre-populates cache for all installed integrations. Allowed filenames: `__init__.py`, `manifest.json`, `config_flow.py`, `const.py`, `strings.json`, and any `*.md` file. See ADR 011.

**Diagnostic WAN verification**: `fetch_url(url)` in `utils/agent/tool_executor.py` is a read-only HTTP GET tool for verifying external service availability (e.g., confirming an API outage has resolved). Governed by `ALLOW_DIAGNOSTIC_WAN` (default true). Block list covers RFC-1918, loopback, and link-local ranges. Registered in all three agent registries. Never use it to POST data or call HA's own API — use `run_ha_command` or `HARestClientProtocol` for that. See ADR 016.

**Chat tool parity**: Any enrichment or analysis function callable by an automated pipeline must be callable from chat with the same set of clients. `ToolExecutor` (`utils/agent/tool_executor.py`) is the authority on which clients chat tools can access; adding a new client type means adding a parameter to `__init__` (and a `set_*` deferred-injection method if the value is only available after construction). When a shared function is imported inside a `ToolExecutor` method, it must receive the same arguments as the automated caller — never `ws_client=None` or similar stubs. Example: `enrich_http_login()` is called identically from the automated notification pipeline and from `_investigate_device` via `self._ws_client`. See ADR 017.

**REST client in ToolExecutor**: `ToolExecutor` holds an optional `HARestClientProtocol` injected via `set_rest_client()`. `main.py` builds `HARestClient(HA_HOST, HA_API_PORT, HA_API_TOKEN)` once and injects it into `_shared_executor` and into every sub-agent executor (chat, log monitor, lovelace, notification, update, investigation loop, config analysis). Tools that need REST: `get_entity_history`, `get_logbook`, `render_ha_template`, `search_integrations` (services), `get_integration_diagnostics`, `reload_integration`, `call_service`, `get_text`. Any new REST-backed tool must follow the chat-parity rule: inject the same client into all executor construction sites.

**HA event subscriber**: `HAEventSubscriber` (`utils/ha/ha_event_subscriber.py`) is a persistent WebSocket connection separate from the per-call `HAWebSocketClient`. It subscribes to `state_changed`, `repairs_issue_registry_updated`, `automation_triggered`, and `persistent_notification/subscribe`, filters and buffers events in a ring buffer (`collections.deque(maxlen=HA_EVENT_BUFFER_SIZE)`, default 500), and publishes to the supervisor bus for the dashboard timeline. The supervisor task `ha_event_subscriber` is gated by `HA_EVENT_SUBSCRIBE` (bool, default true when a token is set). `HAEventSubscriberProtocol` is in `interfaces.py`; `FakeHAEventSubscriber` is beside the real class. `get_recent_events(event_type, entity_id, limit)` reads from the ring buffer (ha, chat, MCP). See ADR 033.

**Event-driven supervisor wake**: `LoopSupervisor.wake(name)` sets a per-loop `asyncio.Event` that `supervised_sleep` waits on alongside the timer. It interrupts the sleep only — unlike `run_now`, it never cancels a running iteration. A wake that arrives mid-iteration stays latched and fires on the next sleep. The subscriber → wake map (5 s debounce): persistent notification change → `notification_poll`; `repairs_issue_registry_updated` → `repair_poll`; `update.*` state change → `update_check` (only when the entity state or `attributes.latest_version` changed — attribute-only mutations such as `in_progress` and `update_percentage` that occur every few seconds during an install are ignored by `_is_update_wake_worthy` in `main.py`). While the subscriber is healthy, those loops sleep `HA_EVENT_FALLBACK_POLL_MINUTES` (default 60) instead of their normal interval; they revert immediately on disconnect. Polling remains the safety net — correctness never depends on the socket. See ADR 033.

**Service policy**: `classify_service_risk(domain, service) -> RiskLevel | None` in `utils/ha/service_policy.py` gates `call_service`. `None` means blocked (no approval path). Risk tiers — LOW: `light`, `switch`, `fan`, `cover`, `input_*`, `notify`, `persistent_notification`; MEDIUM: `automation.*`, `script.*`, `scene.*`, `homeassistant.reload_*`, `homeassistant.update_entity`; HIGH: everything else. Permanently blocked: `homeassistant.restart`/`stop`, `hassio.*`, `backup.*`, `recorder.purge*`, `update.install`, `shell_command.*`, `python_script.*`, `pyscript.*`. Companion-integration services that are destructive or hide problems are also permanently blocked: `homeassistant.delete_all_orphaned_entities`, `homeassistant.disable_user`, `homeassistant.enable_user`, `repairs.ignore_all`, `repairs.unignore_all`, `repairs.remove`, `recorder.import_statistics`. `call_service` is in ha and chat registries; **never** MCP. It routes through `gate.queue_for_approval(risk=...)` → `CARD_TYPE_SERVICE_CALL` → `WorkItem` (ADR 026) to enforce the no-concurrent-HA rule.

**Opportunistic HACS companion integrations**: Pueo detects optional HACS integrations via `HAEnvironmentProfile` (`spook_installed`, `upgrade_advisor_installed`) and uses their data when present, degrading gracefully when absent. Two companion integrations are currently recognised. **Spook** (domain `spook`): `get_spook_issues` reads repairs filtered to `domain="spook"`, groups by `issue_domain`, and passes output through `truncate_to_budget`. Registered in all three non-code-proposal registries and in MCP. The WS call it relies on is a standard HA repairs WebSocket message (`repairs/list_issues`), not a Spook-specific API. **ha-upgrade-advisor** (by brianegge): `utils/ha/upgrade_advisor.py::read_advisor_report()` reads `sensor.upgrade_advisor_status` + `sensor.upgrade_advisor_risk` via REST. The report is used only when `state == "report_ready"` and `available_version == target_version`; otherwise silently ignored. Output is labelled "third-party LLM analysis — verify independently" before injection into the update-analysis context. Companion-integration services that are destructive or audit-hiding are permanently blocked in `service_policy.py` (see Service policy above). See ADR 034.

**Pueo MCP server**: `utils/mcp/pueo_mcp_server.py` exposes a curated read-heavy subset of chat-registry tools as an MCP server on `MCP_PORT` (default 8765). The server runs as a separate Starlette ASGI app started by `LoopSupervisor`; it binds to `0.0.0.0` so HA can reach it over the LAN. All `mcp` SDK imports are deferred into method bodies so Pueo starts normally when the package is absent. Current tool set: 21 read-only tools (see `_MCP_TOOL_NAMES` in `utils/mcp/pueo_mcp_server.py`) covering entity/log/history queries, knowledge ops, HA introspection, and template rendering. To add a tool: add its name to `_MCP_TOOL_NAMES`, add its `ToolDefinition` constant to `_build_asgi_app`'s tool list, add a dispatch test. Never expose write/repair/code tools via MCP (`apply_fix`, `run_ha_command`, `propose_patch`, `call_service`, `reload_integration`, etc.). See ADR 028.

**Correctness over speed**: Pueo prioritizes complete, accurate diagnosis and repair over fast responses. A slow LLM is not an error — it is expected behavior on home-automation hardware. Timeouts should fire only when something is genuinely stuck (LLM server hung, process crashed, infinite generation loop), not when the LLM is merely slow. All agent loops run to completion unless an LLM call stalls beyond the expectation-based threshold. See ADR 022.

**Adaptive per-call LLM timeout**: Each `chat_with_tools` call in `AgentLoop._loop_body` is wrapped with `asyncio.wait_for(timeout=_per_call_timeout_seconds())`. The timeout is computed by `utils/llm/llm_stats.expected_timeout_ms()` as P95(recent_latency_ms) × `AGENT_PER_CALL_TIMEOUT_FACTOR` (default 5×), clamped to `[AGENT_PER_CALL_MIN_TIMEOUT_SECONDS, AGENT_PER_CALL_MAX_TIMEOUT_SECONDS]` (default 5–30 min). Every successful call records its wall-clock latency to the `llm_calls` SQLite table (V25 migration) via `record_llm_call()`, called with `asyncio.create_task(asyncio.to_thread(...))` to avoid blocking the event loop. With fewer than 5 samples the default is 10 minutes. A stalled call propagates `asyncio.TimeoutError` out of the loop as `outcome = "stuck"`. `OllamaClient.chat_with_tools()` also returns `_ollama_timing` (eval_ms, load_ms from Ollama's native fields) which `AgentLoop` records separately so model-load spikes don't inflate the latency percentile. See ADR 022.

**Ollama model capability evaluation**: `utils/llm/model_options.py::derive_call_options()` translates model capabilities (detected by `_check_model_caps()` in `utils/disk/hardware.py`) into the correct Ollama call parameters for each use case. `AgentLoop` calls `_derive_loop_call_options()` once per session. Model selection is dynamic: any model with `tools` in `ollama show` Capabilities that fits in RAM is scored by parameter count (B) + 2.0 bonus for `has_thinking` — no static allowlist. Key parameters: `think=False` in production (prevents Ollama issue #17617 where leaked `</think>` in history locks agentic clients in infinite loops); `num_ctx` derived from model context_length + available RAM; `keep_alive="30m"` in supervisor mode by default (`OLLAMA_IDLE_UNLOAD_MINUTES`; model unloads after N idle minutes, cold reload ~4 s; set to 0 for forever-loaded behavior); `temperature=model_recommended` only when thinking is active (0.0 otherwise). Config keys: `OLLAMA_THINK_MODE` ("auto"|"off"|"low"|"medium"|"high"), `OLLAMA_NUM_CTX` (0=auto-derive), `OLLAMA_KEEP_ALIVE` ("auto"), `OLLAMA_IDLE_UNLOAD_MINUTES` (default 30; 0=keep forever). See ADR 027.

## Configuration

`config.py` loads `config.yaml` at import time and exposes all settings as typed module-level constants with fallback defaults. Agent scripts are run-able directly without a `config.yaml` (defaults kick in); `main.py` is needed to point at a non-default config path. When no `config.yaml` is present, path defaults resolve to platform directories via `paths.get_dirs()` (e.g. `~/Library/Application Support/Pueo/ha_agent_state.db` on macOS). Pass `--config /path/to/config.yaml` or set `PUEO_CONFIG` to override the config file location.

`config.yaml` is gitignored. `config.yaml.default` is the committed reference template. Run `setup.sh` to generate `config.yaml` interactively.

## Deployment

`setup.sh` supports three deployment modes — `macos`, `docker`, or `both` — chosen interactively at setup time. Both macOS and Docker are equally supported; neither is second-class. For Docker, `setup.sh` generates `docker-compose.yml` with the SSH key volume mount (`<host-key-path>:/root/.ssh/id_ed25519:ro`) and writes `config/config.yaml` (the bind-mount source). Run `./setup.sh` and choose the mode; no manual editing of `docker-compose.yml` is needed. `docker-compose.yml.example` is the committed reference template (with placeholders); `docker-compose.yml` is gitignored.

`setup.sh --clean` removes all state (DB, caches, launchd plists, CLI symlink, config.yaml). `setup.sh --reset` does the same but preserves `config.yaml` for a clean reinstall without re-answering questions.

`Dockerfile` + `docker-compose.yml` use `network_mode: host` for ARP/raw socket access. The container uses five volumes: `/config` (bind-mount of `./config/`, read-only — place `config.yaml` here), `/data` (`pueo-data` named volume — backups, ChromaDB), `/state` (`pueo-state` named volume — SQLite DB, HITL cards, tools), `/cache` (`pueo-cache` named volume — scraped knowledge), `/logs` (`pueo-logs` named volume — log files). The `Dockerfile` sets `PUEO_CONFIG_DIR=/config` and equivalent `PUEO_*` env vars; `paths.py` picks these up automatically so no code path references `/app`. `main.py` is the unified entry point; default mode is `supervisor` (same as running `python main.py` directly).

## Testing

When adding or modifying any feature, add corresponding tests in the same session — do not defer them. Tests live in `tests/`, grouped by subsystem. Three tiers:

| Tier | Location | Needs | When to run |
|---|---|---|---|
| Unit | `tests/` | Nothing | Every push (CI enforced) |
| Seam | `tests/integration/` (non-ollama) | Nothing | Locally, on demand |
| Eval | `tests/integration/test_evals.py` | Ollama running | Locally, on demand |

**Rules:**
- Every new Pydantic schema gets three tests: valid construction, invalid/missing fields, JSON round-trip
- Every new `config.py` key gets a test in `TestConfigDefaults` using the `isolated_config` fixture
- Every new pure-logic function (path derivation, regex, threshold comparison) gets a test
- SSH and Ollama calls are never mocked in the unit suite — those are integration/eval concerns
- Use `/project:write-tests <target>` to generate tests for a specific function or module

See `tests/CLAUDE.md` for full commands and the three-tier structure.

The Stop hook (`/.claude/hooks/stop.sh`) will remind you at session end if Python files were modified without touching `tests/`.

## CI

`.github/workflows/test.yml` runs on Python 3.12, 3.13, 3.14 against `main`. Gates: `black`, `flake8` (errors only), `mypy`, `bandit`, `pytest --cov --cov-fail-under=90 --ignore=tests/integration`.

`tests/integration/` is never run on GitHub — it contains seam tests (cross-module state flows) and eval tests (real Ollama inference) that require local services.

## Development Procedure

Every code change follows this procedure in order. Never commit directly to `main`.

### Before writing any code
1. `git checkout main && git pull` — start from a clean base
2. `git remote prune origin` — remove stale remote-tracking refs
3. `git branch --merged | grep -v '^\*\|main' | xargs git branch -d 2>/dev/null` — prune merged local branches
4. **Every change needs a GitHub issue.** Check that an issue exists for this work. If not, create one with `gh issue create` before touching any files. No issue = no branch. Reference the issue number in the branch name and in every commit message.
5. **Plan non-trivial changes first.** Trivial = a few files within the same module; implement directly. Non-trivial = crosses module boundaries or touches many files; agree on the approach before touching any files.
6. `git checkout -b feat/<issue-number>-<slug>` — branch created before the first edit, named with the issue number. If a change was already made on `main` without branching, do this retroactively — uncommitted changes carry over.

### During coding
7. **Write/update tests in the same session** — not deferred. Do not commit logic changes without corresponding test changes.
8. **Update all related files** and report explicitly when done:
   - Config key added → `config.py`, `config.yaml.default`, and `setup.sh`
   - Architecture change → add/update a decision record in `docs/decisions/`
   - Public interface changed → update this file if the pattern is documented here
   - Dependency added/changed → update `requirements.txt` or `requirements-dev.txt`
9. **Migrations and schema changes** — flag separately from code changes. Test against a real local copy of `ha_agent_state.db`. Document the rollback path (which migration version to revert to) before proceeding.
10. **Security review** — invoke `/security-review` when the change meaningfully touches SSH transport, external HTTP calls, credential handling, or production file writes.

### Before committing
11. `git diff --staged` — self-review the diff; catch noise, debug artifacts, unintended changes
12. **Exercise the change** — run the app and manually verify the affected behavior. Tests confirm code correctness; only running it confirms the feature works. Use `/run` to launch the app. If the change touches the dashboard, SSH flows, or LLM interactions, test those paths directly. If it truly cannot be exercised locally (e.g. requires live HA state that isn't available), say so explicitly rather than skipping silently.
13. Commit atomically — one logical concern per commit; message explains *why*, not *what*; include `Closes #N` or `Refs #N`.

### Before opening a PR
14. Run the full CI gate locally — all must pass:
    ```bash
    black --check .
    flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
    mypy --ignore-missing-imports .
    bandit -r . -x ./tests,./.venv
    pytest --cov --cov-fail-under=90 --ignore=tests/integration
    ```
15. **Rollback planning** — for migrations or config writes to production, note the rollback path in the PR description (revert commit + migration version).
16. CI passing = done, open the PR. `gh pr create` — description focuses on *why*, not *what*; reference the issue (`Closes #N`); include rollback note if step 15 applies.

### After merge
17. Repeat steps 1–3 to clean up.

## Roadmap

@docs/roadmap-summary.md

## MCP Servers

`.mcp.json` configures a Home Assistant MCP server for use during development, giving Claude Code direct access to live HA state and entities. Requires `mcp-homeassistant` installed (`uvx mcp-homeassistant`) and `HA_TOKEN` set in the environment. See `.mcp.json` for the full config shape.

## Knowledge Base

`pueo-kb` ([AndysWorth/pueo-kb](https://github.com/AndysWorth/pueo-kb)) is the
federated runbook library. Pueo pulls from it at every RAG refresh
(`--mode rag-refresh`) via `utils/knowledge/kb_ingester.py` and contributes
reviewed runbooks back via `utils/knowledge/kb_contributor.py`.

Configured via `PUEO_KB_REPO` in `config.yaml` (default `"AndysWorth/pueo-kb"`).
Seed runbooks in `prompts/seed_*.md` are mirrored there under `runbooks/`.

## Work Tracking

New bugs, enhancements, and feature work are tracked in [GitHub Issues](https://github.com/AndysWorth/pueo/issues). Use labels `bug`, `enhancement`, `security`, `ux`, or `discussion`.

When starting work on an issue, reference it in the branch name (`feat/<slug>`) and in commit messages (`Closes #N`). Complex features that need upfront design get a spec in `docs/plan/` — link it from the issue before implementing.

The initial build-out phases (items 1–STOR-6) are archived in `docs/implementation-plan.md` as a historical record.

## Design Decisions

Rationale for key architectural choices is in `docs/decisions/`:

@docs/decisions/000-index.md
