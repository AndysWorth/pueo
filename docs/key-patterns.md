# Pueo — Key Patterns

Authoritative patterns for all code changes. Read this before modifying any agent, tool executor, or infrastructure path.

## Structured LLM output
All Ollama calls use `format=PydanticModel.model_json_schema()` and `temperature=0.0` to force deterministic, parseable JSON. Always wrap in `asyncio.to_thread()` since `ollama.chat` is synchronous.

## Safety invariant
No write operation proceeds without a confirmed HA backup slug. Ordering is always `execute_remote_backup()` → `record_backup_slug()` → remediation. Never bypass this chain.

## Long-lived clients, bounded FDs
LLM and embedding clients are created once and injected; never construct one per call or per log line (each opens an HTTP pool). `main.py::_raise_fd_limit()` lifts the soft `RLIMIT_NOFILE` to 4096 at startup (the launchd plist sets `NumberOfFiles` too), both uvicorn servers set `limit_concurrency`, and `resource_poll` logs `open_fds`/`fd_limit` plus `fd_warn` above 80% — checked before SSH so it still reports when FD exhaustion is why SSH fails (#769).

## SSH connections
Each function opens its own `asyncssh.connect()` context. `known_hosts=None` is intentional for local-network HA hosts — flag in any security review.

## Single config source
`config.py` is the only place settings are defined. Agent scripts must import from it (`from config import ...`) and must never redeclare constants. Adding a new setting means adding it to `config.yaml.default`, `config.py`, and `setup.sh` — nowhere else. `setup.sh` also generates `docker-compose.yml` for Docker deployments — changes to the compose file structure belong in `setup.sh`'s heredoc, not in `docker-compose.yml.example` (the committed placeholder template); `docker-compose.yml` is gitignored because setup.sh generates it with the user's SSH key path embedded.

## Config path resolution
`config.py` loads at module import time. It checks the `PUEO_CONFIG` environment variable first, then falls back to `config.yaml` next to the script. `main.py` sets `PUEO_CONFIG` before importing any agent module so the right config file is used. Agent imports inside `main.py` must stay deferred (inside the `if args.mode` blocks) — moving them to the top of the file would break this.

## Platform directory abstraction
`paths.py` at the repo root provides `PueoDirectories` (frozen dataclass) and `get_dirs()` factory. `main.py` calls `get_dirs().create_all()` early in startup. All mutable paths (DB, logs, HITL, backups, ChromaDB, tools, caches) derive from `get_dirs()` — never from `config.py` defaults alone. Override any directory with `PUEO_DATA_DIR`, `PUEO_STATE_DIR`, `PUEO_CACHE_DIR`, `PUEO_LOG_DIR`, `PUEO_CONFIG_DIR` env vars; the `Dockerfile` sets these to `/data`, `/state`, etc. for Docker. `resources_dir` (prompts, web assets, deploy templates) is always `Path(__file__).parent` of `paths.py` — immutable, not overridable. Tests use the `pueo_dirs` fixture (in `tests/conftest.py`) which sets `PUEO_*` env vars to `tmp_path` subdirectories so no test writes to `~/Library/`.

## Prompt loading via importlib.resources
`prompts/` is a proper Python package (`__init__.py` present, declared in `pyproject.toml`). `utils/core/prompts.py::load_prompt()` uses `importlib.resources.files("prompts")` — works correctly in editable installs, wheels, and macOS `.app` bundles. `web/templates/` and `web/static/` still use `paths.get_dirs().resources_dir` because FastAPI's `StaticFiles`/`Jinja2Templates` require `Path` objects, not `Traversable`.

## Sandbox path derivation
`SANDBOX_REMOTE_DIR` and `SANDBOX_REMOTE_FILE` in `agents/ha_agent_sandbox_engine.py` are derived from `CONFIG_REMOTE_PATH`, not independently hardcoded, so changing the config path in `config.yaml` automatically keeps the sandbox path in sync.

## Autonomy gate
`AutonomyGate` in `utils/agent/autonomy.py` is the single approval decision point imported by all Pueo modules. Every action that touches remote state must call `gate.require_approval()` or `gate.should_auto_execute()` — no module may hard-code its own ask/skip logic. `FakeAutonomyGate` is the test double.

## Rate limiter / debouncer
`Debouncer` and `RateLimiter` in `utils/core/rate_limiter.py` govern repair frequency. `DEBOUNCE_WINDOW_SECONDS` collapses rapid identical triggers; `MAX_REPAIRS_PER_HOUR` caps total actions in a rolling window. Both are enforced before any repair pipeline call.

## Stuck-loop backoff
Every `_run_*_investigation` caller writes a 30-minute deferred suppression row (`mark_investigation_backoff` in `utils/hitl/hitl_tracker.py`) for any `outcome != "success"`. This prevents poll loops from immediately re-triggering the same session after a timeout or budget exhaustion. The backoff key format is `<session_type>_backoff:<entity_or_issue_key>`. The lovelace benign gate uses `should_send_card` (not a raw SQL check) so it respects both permanent benign suppression and the time-limited stuck backoff.

## Token budget management
`estimate_tokens()` and `truncate_to_budget()` in `utils/core/context.py` enforce the 8,000-token evaluation matrix constraint. Every Ollama call site must trim content to `MAX_PROMPT_TOKENS` before dispatch — never pass unbounded YAML or log content.

## Dependency injection via Protocol interfaces
`interfaces.py` defines `SSHClientProtocol`, `LLMClientProtocol`, `HARestClientProtocol`, `HAWebSocketClientProtocol`, and `HAEventSubscriberProtocol`. Agent functions accept these optional injected clients, falling back to real implementations when `None`. Tests pass `FakeSSHClient` / `FakeLLMClient` / `FakeHARestClient` / `FakeHAWebSocketClient` / `FakeHAEventSubscriber`; SSH, Ollama, and HA APIs are never called in the unit suite.

## Plain-text console formatter
`_TextFormatter` in `utils/core/logging.py` is used on `stderr` when `setup_logging(console_text=True)` is called. The file handler always stays JSON. `main.py` enables `console_text` for `--mode netalertx-setup` to produce human-readable installer output.

## LLM-guided all actions — 6-phase methodology
Every significant Pueo action flows through LLM tool-calling reasoning via `AgentLoop`. Infrastructure operations that bypass the LLM (scheduled scraper runs, disk-space enforcement, backup retention sweeps) are housekeeping, not decisions. The boundary rule: if a function changes HA state or makes a judgment call about what to do next, it belongs in an agent loop, not a direct call.

All agent sessions follow the **6-phase investigation cycle** (encoded in `prompts/agent_loop.md`; see ADR 018):
1. **Retrieve plan** — call `query_knowledge` first with the question or trigger. The result may include both background context and an investigation plan (runbook). If a runbook is returned, follow it as the starting point. If nothing relevant is returned, record this as a knowledge gap and proceed with first-principles reasoning.
2. **Form a hypothesis** — one sentence before calling any tool
3. **Gather evidence** — `read_config`, `read_logs`, `read_file`, `run_ha_command`, `read_pueo_log`, `fetch_ha_docs`. Call `query_knowledge` again if initial evidence narrows the problem to a specific sub-domain, confidence is low, or the agent is about to try a novel approach.
4. **Confirm root cause** — state it explicitly before acting; call `remember(key="<domain>_quirk", content="...")` for any non-obvious instance-specific fact (device quirk, known-wrong entity state, user preference) worth preserving across sessions
5. **Act** — apply fix or recommend action
6. **Report** — call the terminal tool (`finish_repair`, `finish_chat`, `finish_investigation`)

**Stopping condition**: the agent does not stop because it hit an arbitrary tool count. It stops only when it has genuinely exhausted all reasonable investigative paths. Before calling the terminal tool with `outcome=failed`, the agent must call `request_escalation(reason)` so the user can route to a stronger model. Post-session distillation (ADR 038) captures what was tried as a knowledge gap automatically.

## Hard constraints — no concurrent LLM, no concurrent HA
During supervised operation (when `PueoWorkQueue` is initialized), two rules are absolute: (1) Only one LLM call (`AgentLoop.run()` or any judgment-call LLM use) may run at a time. (2) Only one operation that writes to or restarts Home Assistant (SSH write, REST write, `ha backup new`, `ha core restart`, `ha os update`) may run at a time. Both constraints are enforced by routing through `PueoWorkQueue` — use `get_work_queue_or_none()` + `WorkItem`, fall back to direct `await`/`asyncio.create_task` only in test/standalone mode when the queue is not initialized. This applies to all callers: background monitor loops, the chat endpoint, and card handlers in `web/dashboard.py` (`approve()`, `apply_fixes()`). Card handlers use `priority=PRIORITY_HIGH`, `activity_type="card_execution"`, `dedup_key=f"card_{nid}"` — the dedup key prevents double-approval. One-shot LLM calls exempt from the queue: volume-throttled streaming pre-filters (`analyze_log_line_with_ai`) and secondary enrichment inside an already-running tool executor (`_enrich_fix_context`). Use `AgentLoop` for any LLM interaction that makes a judgment call, touches HA state, or could benefit from iterative evidence gathering. See ADR 018, ADR 025, ADR 026.

## Runbook lifecycle (ADR 038)
Runbooks are distilled post-session by an automatic `WorkItem`, not explicitly saved by the agent. Three states: **seed** (human-curated, embedded at RAG refresh via `strategy_seeder.py`), **validated** (reviewed in the dashboard), and **candidate** (auto-distilled, awaiting review). Failed sessions produce a row in `knowledge_gaps` (never retrieved by agents — developer-facing only). `seed_home_profile()` in `strategy_seeder.py` generates a dynamic `ha_instance_profile` document and upserts it into the `strategies` ChromaDB collection at each RAG refresh (step 5.5). `prompts/seed_supervisor_cli.md` provides a Supervisor CLI reference so agents know which `run_ha_command` calls to use. See ADR 038 (supersedes runbook sections of ADR 018 and ADR 030).

## Developer docs
`ha_developer_docs` is a ChromaDB collection (in `COLLECTIONS` in `knowledge_store.py`) that stores curated pages from `developers.home-assistant.io` (architecture, entity model, config flows, Supervisor API, WebSocket API, REST API). Populated by `utils/knowledge/ha_developer_docs_scraper.py` at RAG refresh (step 3.7). Authority score 1.0 — treated as official docs. Cache lives at `HA_DEVELOPER_DOCS_CACHE_DIR` (default `~/Library/Caches/Pueo/ha_developer_docs/`).

## Repair history
`repair_history` is a ChromaDB collection (in `COLLECTIONS` in `knowledge_store.py`) that stores completed repair episodes as searchable text chunks. `embed_repair_episodes()` in `utils/knowledge/repair_episode_embedder.py` reads rows where `embedded_at IS NULL` from `repair_episodes` SQLite, embeds them via `format_episode_for_embedding()`, and marks `embedded_at`. Called as step 7 of `run_rag_refresh` in `main.py`. `AgentLoop._pre_inject_knowledge()` queries all `COLLECTIONS` including `repair_history`, so similar past repairs automatically appear as context before the first LLM call. See ADR 031.

## HA best practices
`ha_best_practices` is a ChromaDB collection (in `COLLECTIONS` in `knowledge_store.py`) that stores reference files from the [homeassistant-ai/skills](https://github.com/homeassistant-ai/skills) repo: deprecated API tables, YAML guidelines, dashboard patterns, template guidelines, and domain-specific docs. Populated by `utils/knowledge/ha_skills_scraper.py` at RAG refresh (step 8). Authority score 0.9. `_query_knowledge` labels these chunks `[BEST PRACTICE]`. All three `query_type` routes include this collection. See ADR 034.

## Authority-ranked knowledge retrieval
`KnowledgeChunk` carries an `authority_score: float` set at query time. Scores range from 1.0 (official HA docs) down to unreviewed candidates; see ADR 030 for the full tier table. `ChromaKnowledgeStore.query()` uses **hybrid BM25+cosine retrieval** (ADR 029): `score = cosine_sim × 0.7 + bm25_sim × 0.3`. Final ranking: `(authority_score × 0.3) + (score × 0.7)`. Chunks carry optional `ha_version_min`/`ha_version_max`; `query_knowledge` applies a 1.2× boost for matching version and 0.5× penalty for chunks older than 12 months. `query_type` (`"diagnostic"`, `"procedural"`, `"generative"`, `"version_check"`) routes to the most relevant collection subset — see `ToolExecutor._QUERY_TYPE_COLLECTIONS`. See ADR 029, ADR 030.

## Agent self-awareness
`read_source` is registered in all agent registries (`build_ha_tool_registry`, `build_netalertx_tool_registry`, `build_chat_tool_registry`, `build_code_proposal_registry`) in `utils/agent/tool_registry.py`. The LLM can call `read_source("utils/agent/tool_registry.py")` during any session to inspect which tools are available. Safety-critical paths (`utils/agent/autonomy.py`, `interfaces.py`, `config.py`) remain write-blocked by `_SAFETY_CRITICAL_PATHS` in `propose_patch` but are readable. See ADR 010.

## HA live lookup
`fetch_ha_docs(domain, filename)` in `utils/agent/tool_executor.py` fetches HA component source or docs from GitHub raw. In `local` mode it serves from cache only — a cache miss raises `ToolError` and makes no network call. In `cloud`/`both` mode it fetches live and writes to cache. Cache lives at `HA_SOURCE_CACHE_DIR` (default `~/Library/Caches/Pueo/ha_source/`). Allowed filenames: `__init__.py`, `manifest.json`, `config_flow.py`, `const.py`, `strings.json`, and any `*.md` file. See ADR 011.

## Diagnostic WAN verification
`fetch_url(url)` in `utils/agent/tool_executor.py` is a read-only HTTP GET tool for verifying external service availability. Governed by `ALLOW_DIAGNOSTIC_WAN` (default true). Block list covers RFC-1918, loopback, and link-local ranges. Never use it to POST data or call HA's own API. See ADR 016.

## Chat tool parity
Any enrichment or analysis function callable by an automated pipeline must be callable from chat with the same set of clients. `ToolExecutor` (`utils/agent/tool_executor.py`) is the authority on which clients chat tools can access; adding a new client type means adding a parameter to `__init__` (and a `set_*` deferred-injection method if the value is only available after construction). When a shared function is imported inside a `ToolExecutor` method, it must receive the same arguments as the automated caller — never `ws_client=None` or similar stubs. See ADR 017.

## REST client in ToolExecutor
`ToolExecutor` holds an optional `HARestClientProtocol` injected via `set_rest_client()`. `main.py` builds `HARestClient(HA_HOST, HA_API_PORT, HA_API_TOKEN)` once and injects it into `_shared_executor` and into every sub-agent executor (chat, log monitor, lovelace, notification, update, investigation loop, config analysis). Tools that need REST: `get_entity_history`, `get_logbook`, `render_ha_template`, `search_integrations` (services), `get_integration_diagnostics`, `reload_integration`, `call_service`, `get_text`. Any new REST-backed tool must follow the chat-parity rule: inject the same client into all executor construction sites.

## HA event subscriber
`HAEventSubscriber` (`utils/ha/ha_event_subscriber.py`) is a persistent WebSocket connection separate from the per-call `HAWebSocketClient`. It subscribes to `state_changed`, `repairs_issue_registry_updated`, `automation_triggered`, and `persistent_notification/subscribe`, filters and buffers events in a ring buffer (`collections.deque(maxlen=HA_EVENT_BUFFER_SIZE)`, default 500), and publishes to the supervisor bus for the dashboard timeline. `HAEventSubscriberProtocol` is in `interfaces.py`; `FakeHAEventSubscriber` is beside the real class. See ADR 033.

## Event-driven supervisor wake
`LoopSupervisor.wake(name)` sets a per-loop `asyncio.Event` that `supervised_sleep` waits on alongside the timer. It interrupts the sleep only — unlike `run_now`, it never cancels a running iteration. The subscriber → wake map (5 s debounce): persistent notification change → `notification_poll`; `repairs_issue_registry_updated` → `repair_poll`; `update.*` state change → `update_check` (attribute-only mutations like `in_progress` and `update_percentage` are ignored by `_is_update_wake_worthy` in `main.py`). While the subscriber is healthy, those loops sleep `HA_EVENT_FALLBACK_POLL_MINUTES` (default 60) instead of their normal interval. Polling remains the safety net — correctness never depends on the socket. See ADR 033.

## Service policy
`classify_service_risk(domain, service) -> RiskLevel | None` in `utils/ha/service_policy.py` gates `call_service`. `None` means blocked (no approval path). Risk tiers — LOW: `light`, `switch`, `fan`, `cover`, `input_*`, `notify`, `persistent_notification`; MEDIUM: `automation.*`, `script.*`, `scene.*`, `homeassistant.reload_*`, `homeassistant.update_entity`; HIGH: everything else. Permanently blocked: `homeassistant.restart`/`stop`, `hassio.*`, `backup.*`, `recorder.purge*`, `update.install`, `shell_command.*`, `python_script.*`, `pyscript.*`. Companion-integration services that are destructive or audit-hiding are also permanently blocked. `call_service` is in ha and chat registries; **never** MCP. It routes through `gate.queue_for_approval(risk=...)` → `CARD_TYPE_SERVICE_CALL` → `WorkItem` (ADR 026) to enforce the no-concurrent-HA rule.

## Opportunistic HACS companion integrations
Pueo detects optional HACS integrations via `HAEnvironmentProfile` (`spook_installed`, `upgrade_advisor_installed`, `ai_agent_ha_installed`) and uses their data when present, degrading gracefully when absent. **Spook** (domain `spook`): `get_spook_issues` reads repairs filtered to `domain="spook"`, groups by `issue_domain`, passes output through `truncate_to_budget`. **ha-upgrade-advisor** (by brianegge): `utils/ha/upgrade_advisor.py::read_advisor_report()` reads `sensor.upgrade_advisor_status` + `sensor.upgrade_advisor_risk` via REST; used only when `state == "report_ready"` and `available_version == target_version`; output labelled "third-party LLM analysis — verify independently". **ai_agent_ha** (by sbenodiz, domain `ai_agent_ha`): automations use ids prefixed `ai_agent_auto_`; `set_entity_state` fallback to `hass.states.async_set` creates phantom state for non-standard domains (light/switch/cover/climate/fan are fine; everything else is phantom). Companion-integration destructive/audit-hiding services are permanently blocked in `service_policy.py`. See ADR 034.

## Pueo MCP server
`utils/mcp/pueo_mcp_server.py` exposes a curated read-heavy subset of chat-registry tools as an MCP server on `MCP_PORT` (default 8765). The server binds to `0.0.0.0` so HA can reach it over the LAN. Current tool set: 22 read-only tools (see `_MCP_TOOL_NAMES`). To add a tool: add its name to `_MCP_TOOL_NAMES`, add its `ToolDefinition` constant to `_build_asgi_app`'s tool list, add a dispatch test. Never expose write/repair/code tools via MCP. See ADR 028.

## Correctness over speed
Pueo prioritizes complete, accurate diagnosis and repair over fast responses. A slow LLM is not an error — it is expected behavior on home-automation hardware. Timeouts should fire only when something is genuinely stuck (LLM server hung, process crashed, infinite generation loop), not when the LLM is merely slow. See ADR 022.

## Adaptive per-call LLM timeout
Each `chat_with_tools` call in `AgentLoop._loop_body` is wrapped with `asyncio.wait_for(timeout=_per_call_timeout_seconds())`. The timeout is computed by `utils/llm/llm_stats.expected_timeout_ms()` as P95(recent_latency_ms) × `AGENT_PER_CALL_TIMEOUT_FACTOR` (default 5×), clamped to `[AGENT_PER_CALL_MIN_TIMEOUT_SECONDS, AGENT_PER_CALL_MAX_TIMEOUT_SECONDS]` (default 5–30 min). Every successful call records wall-clock latency to the `llm_calls` SQLite table (V25 migration) via `record_llm_call()`. A stalled call propagates `asyncio.TimeoutError` as `outcome = "stuck"`. See ADR 022.

## Ollama model capability evaluation
`utils/llm/model_options.py::derive_call_options()` translates model capabilities (detected by `_check_model_caps()` in `utils/disk/hardware.py`) into the correct Ollama call parameters for each use case. Model selection is dynamic: any model with `tools` in `ollama show` Capabilities that fits in RAM is scored by parameter count (B) + 2.0 bonus for `has_thinking` — no static allowlist. Key parameters: `think=False` in production (prevents Ollama issue #17617 where leaked `</think>` in history locks agentic clients in infinite loops); `keep_alive="30m"` in supervisor mode by default. Config keys: `OLLAMA_THINK_MODE` ("auto"|"off"|"low"|"medium"|"high"), `OLLAMA_NUM_CTX` (0=auto-derive), `OLLAMA_KEEP_ALIVE` ("auto"), `OLLAMA_IDLE_UNLOAD_MINUTES` (default 30; 0=keep forever). See ADR 027.
