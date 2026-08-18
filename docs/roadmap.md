# Pueo — Development Roadmap

## Milestone Status

### Milestones

Strategic capabilities in delivery order.

| Milestone                                     | Status                  | Code Location                              |
| --------------------------------------------- | ----------------------- | ------------------------------------------ |
| 1. Read-only ingestion & diagnostics          | ✅ Complete              | `ha_agent_core.py`                         |
| 2. Local RAG & knowledge ingestion            | ✅ Complete (2026-07-28) | `utils/knowledge_store.py`, `utils/ha_release_notes_scraper.py`, `utils/hacs_scraper.py` |
| 3. Safe execution / shadow mode               | ✅ Complete              | `ha_agent_sandbox_engine.py`               |
| 4. Closed-loop autonomous healing             | ✅ Complete              | `ha_agent_sandbox_engine.py`               |
| 4.5. HA Resource Stewardship                  | ✅ Complete (2026-07-27) | `ha_agent_advanced.py`, `web/dashboard.py` |
| 4.6. HA Update Manager                        | ✅ Complete (2026-07-27) | `utils/ha_rest_client.py`                  |
| 4.7. HA Notification Intelligence             | ✅ Complete (2026-07-27) | `utils/ha_ws_client.py`, `web/dashboard.py`|
| 4.8. HA Repairs & Update Orchestration        | ✅ Complete (2026-08-05) | `ha_update_manager.py`, `ha_log_monitor.py`|
| 5. Agent quality & evaluation                 | ✅ Complete (2026-07-28) | `evals/`                                   |
| 6. Tool-calling agent loop                    | ✅ Complete (2026-07-28) | `utils/agent_loop.py`                      |
| 6.5. Supervisor + Active Dashboard            | ✅ Complete (2026-07-30) | `main.py`, `web/dashboard.py`              |
| 6.6. Conversational Agent                     | ✅ Complete (2026-07-31) | `web/templates/chat.html`, `utils/tool_executor.py` |
| 7. Configurable LLM Provider + Cloud Escalation | ✅ Complete (2026-08-07) | `utils/cloud_client.py`, `utils/llm_factory.py`, `utils/billing.py` |
| 8. Repair episode recording                   | ✅ Complete (2026-08-10) | `utils/repair_episode.py`, `utils/anonymizer.py` |
| 9. Federated case library                     | ✅ Complete (2026-08-11) | `utils/case_submitter.py`, `utils/case_ingester.py` |
| 10. Self-improving code proposals *(stretch)* | ✅ Complete (2026-08-11) | `utils/tool_executor.py`, `utils/agent_loop.py` |
| 11. Transparent operation                     | ✅ Complete (2026-08-18) | `utils/agent_loop.py`, `web/dashboard.py`, `web/templates/chat.html`, `web/templates/overview.html` |

### Implementation Phases

Tactical delivery batches in execution order. See `docs/implementation-plan.md` for item-level detail.

| Phase                                              | Status                  | Items   |
| -------------------------------------------------- | ----------------------- | ------- |
| Phase 1–3: Foundation, Observability, Architecture | ✅ Complete (2026-07-15) | 1–9     |
| Phase 3.5: Autonomy Control                        | ✅ Complete (2026-07-19) | 9.5     |
| Phase 4: NetAlertX Integration                     | ✅ Complete (2026-07-20) | 10–19   |
| Phase 4.5: Approval Web Dashboard                  | ✅ Complete (2026-07-20) | 19.5    |
| Phase 5: Observability UX                          | ✅ Complete (2026-07-20) | 20      |
| Phase 6: Installer Intelligence                    | ✅ Complete (2026-07-21) | 21–22   |
| Phase 7: Evidence Capture & Approval Display       | ✅ Complete (2026-07-21) | 23–24   |
| Phase 8: NetAlertX Compatibility Maintenance       | ✅ Complete (2026-07-21) | 25      |
| Phase 9: NetAlertX One-Shot Diagnosis              | ✅ Complete (2026-07-22) | 27      |
| Phase 11: Resource Stewardship                     | ✅ Complete (2026-07-27) | 28–32   |
| Phase 12: HA Update Manager                        | ✅ Complete (2026-07-27) | 33–37   |
| Phase 12.5: HA Repairs & Update Orchestration      | ✅ Complete (2026-08-05) | 12.5A–B |
| Phase 13: HA Notification Intelligence             | ✅ Complete (2026-07-27) | 38–41   |
| Phase 14: Tool-Calling Agent Loop                  | ✅ Complete (2026-07-28) | 42–48   |
| Phase 15: RAG Knowledge Layer                      | ✅ Complete (2026-07-28) | 49–52   |
| Phase 16: Evals                                    | ✅ Complete (2026-07-28) | 53–54   |
| Phase 17: Supervisor + Active Dashboard            | ✅ Complete (2026-07-30) | 55–64   |
| Phase 17.5: Conversational Agent                   | ✅ Complete (2026-07-31) | 65–72   |
| Phase 18: Configurable LLM Provider + Cloud Escalation | ✅ Complete (2026-08-07) | 73–76   |
| Phase 19: Repair Episode Recording                 | ✅ Complete (2026-08-10) | 77–79   |
| Phase 20: Federated Case Library                   | ✅ Complete (2026-08-11)    | 80–82   |
| Phase 21: Code Proposals *(stretch)*               | ✅ Complete (2026-08-11) | 83–86   |
| Phase 22: HA RAG Strategy                          | ✅ Complete (2026-08-06) | 87–93   |
| Phase 23: Disk Usage Tab                           | ✅ Complete (2026-08-07) | DU-1–6  |

---

## Remaining Work

**Execution order:** 4.6 → 4.7 → 6 → 2 → 5 → **6.5** → **6.6** → 7 → 8 → 9 → 10*(stretch)*. The milestone numbers reflect original sequencing; the phases deliver them in this order. See `docs/implementation-plan.md` for item-level detail.

---

### Milestone 6.5 — Supervisor + Active Dashboard

**Delivered:** 2026-07-30 (Phase 17, items 55–64)

`python main.py` (no flags) is now the single entry point. It starts all monitoring loops
(HA log tail, resource polling, update checks, notification polling, NetAlertX) and the
dashboard in one supervised asyncio process. `LoopSupervisor` wraps each task with
exception catching and exponential-backoff restart (2s → 5-min cap); a crashed loop emits a
`loop_error` SSE event and restarts automatically.

Approving a card executes repair actions in-process via a `card_type` dispatch table —
no more file-polling race where the one-shot process exits before the user approves. The
dashboard (`http://127.0.0.1:<DASHBOARD_PORT>`) shows real-time loop health, a live event
timeline with drill-down, resource gauges, a configuration editor with live-apply for runtime
params, and loop pause/resume/run-now controls. A launchd plist keeps Pueo alive at login and
restarts it on crash. `--mode audit` produces a structured gap report saved to `audits/`.

Full spec: [plan/supervisor.md](plan/supervisor.md)

---

### Milestone 6.6 — Conversational Agent

**Objective:** Add a Chat tab to the dashboard so the user can talk directly to Pueo — querying live HA state, storing persistent notes across sessions, and proposing new tools through a sandboxed code flow. The same `AgentLoop` that drives reactive repair sessions drives the conversational agent; only the system prompt, tool registry, and termination signal differ.

**Why here:** Pueo is currently reactive only. Adding conversation lets the user interrogate the system at any time ("what's the state of sensor X?"), build up context that informs future repairs ("remember that the NAS is on 192.168.1.100"), and extend Pueo's capabilities without editing source code. The code-sandbox path (items 70–71) also delivers the shared infrastructure that Milestone 10 (Phase 21) reuses for its autonomous code-proposal flow.

**Key design choices:**
- `AgentLoop` is reused unchanged, extended with a `terminal_tool_name` parameter (defaults to `"finish_repair"` to preserve existing behavior)
- Memory uses SQLite keyword search — no new embedding overhead; ChromaDB can be wired in later
- Chat responses stream via a dedicated `/chat/events` SSE endpoint (separate from the global `/events` stream)
- `add_tool` requires sandbox pass + explicit approval regardless of autonomy level — hardcoded, not gated by `AutonomyGate`
- `CHAT_ALLOW_TOOL_REGISTRATION = false` default — inert until the user explicitly enables it

**Tasks (Phase 17.5, items 65–72):**
- DB migration v8: `agent_memory`, `chat_sessions`, `chat_messages` tables
- `remember`/`recall` tools + `CHAT_MEMORY_TOP_K`, `CHAT_ALLOW_TOOL_REGISTRATION` config keys
- `build_chat_tool_registry()`, `finish_chat` tool definition, `AgentLoop.terminal_tool_name` parameter
- `/chat` dashboard route + `chat.html` template (session list, message thread, input)
- `POST /chat/message` endpoint + `GET /chat/events` SSE; `asyncio.create_task` loop dispatch
- `read_source`, `propose_patch`, `sandbox_code` tools (shared with Milestone 10)
- `add_tool`: DB migration v9 (`registered_tools`), dynamic tool executor, `code_proposal` approval card
- Tests: `test_chat.py` full coverage

**Validation gate:** `/chat` tab accessible in dashboard; "What is the HA disk usage?" triggers `run_ha_command` and returns a human-readable answer; "remember that X" stores a memory; memory survives page reload; with `CHAT_ALLOW_TOOL_REGISTRATION=true`, a proposed tool clears sandbox and appears as a approval card; approving it makes the tool callable in the next session.

Full spec: [plan/conversational-agent.md](plan/conversational-agent.md)

---

### Milestone 4.6 — HA Update Manager

**Objective:** Detect available Home Assistant Core, OS, and add-on updates during normal monitoring; evaluate whether each update is safe for this specific installation using LLM analysis of release notes; execute updates with the backup invariant intact; verify Pueo's own integration still works after a Core update.

**Why here:** HA ships breaking changes regularly — CLI command renames, config YAML deprecations, REST API shifts. Without this capability, Pueo has no way to know an update is available or whether it will break something it depends on. Pueo has already been broken once by a CLI rename (`ha addons` → `ha apps`). The self-check closes that loop.

**Key design choices:**
- Update availability read from `update.*` REST state entities — no WebSocket, no SSH parsing
- Breaking-change analysis is **advisory only** — never a hard gate; human decides
- Release notes fetched from GitHub once per version and cached locally — no WAN during active monitoring
- Core and OS updates always require approval regardless of autonomy level
- Add-on updates are MEDIUM risk and may auto-execute at autonomy level 4
- `execute_remote_backup()` runs before every update (safety invariant unchanged)

**Tasks:**
- New `HARestClient` implementing `HARestClientProtocol`; `FakeHARestClient` for tests
- Poll `update.*` entities via REST; `UpdateStatus` dataclass; `--mode update-check` CLI entry point
- Fetch + cache HA release notes; `UpdateReadinessReport` Pydantic schema; LLM advisory analysis
- Update approval cards with per-component approval and advisory breaking-changes section
- `ha core update`, `ha os update`, Supervisor API add-on updates — all with backup invariant
- Post-update: `ha core check`, log triage, Pueo command catalog smoke-test, LLM cross-reference

**Validation gate:** `--mode update-check` correctly identifies an available Core update; breaking-change analysis flags a known deprecated config key; approval card appears and requires approval; update executes with backup; self-check passes.

Full spec: [plan/ha-update-manager.md](plan/ha-update-manager.md)

---

### Milestone 4.7 — HA Notification Intelligence

**Objective:** Surface HA persistent notifications (failed logins, config errors, integration failures) as approval-ready cards with plain-English explanations, enriched context, and clear recommended actions — rather than leaving them as raw technical strings in the HA UI.

**Why here:** HA notifications are Pueo's early warning system. A failed login from an unknown IP, a broken integration, or a config error all appear as notifications before they become active incidents. Pueo can add value here without any repair capability — just explanation and triage.

**Key design choices:**
- `persistent_notification.*` entities are REST-pollable (no WebSocket needed for listing)
- For `http_login` (failed auth): extract source IP, enrich with reverse DNS + NetAlertX device name + HA device registry; unknown-source logins escalated to CRITICAL
- LLM explains each notification in plain English and recommends action
- Dismissal only on explicit user action — never auto-dismissed
- `notification_history` SQLite table prevents repeat approval cards for the same notification

**Tasks:**
- Poll `persistent_notification.*` REST entities on configurable interval
- `NotificationAnalysis` Pydantic schema; `notification_history` SQLite migration
- IP enrichment: reverse DNS (`socket.gethostbyaddr`) + NetAlertX `/devices` + HA device registry via WebSocket
- Per-notification approval cards; dismiss service call on approval
- Notifications tab in dashboard: pending, history, filters
- `--mode notifications` one-shot CLI entry point

**Validation gate:** A simulated `http_login` notification generates a approval card with enriched device name; an unknown-IP login is escalated to CRITICAL; dismissal fires the HA dismiss service; notification history prevents duplicate cards.

Full spec: [plan/ha-notifications.md](plan/ha-notifications.md)

---

### Milestone 6 — Tool-Calling Agent Loop

**Objective:** Replace the linear `gather→analyze→act` pipeline with an iterative agent loop using Ollama's `tools` API. The model decides which tools to call at each step, iterates until it reaches a confident fix or exhausts its budget, and can investigate unknown failure modes rather than only pre-scripted ones.

**Tasks:**
- Define tool registry (`utils/tool_registry.py`): `read_config`, `read_logs`, `run_ha_command`, `read_file`, `query_netalertx`, `apply_fix`, `verify_fix`, `finish_repair` — all as Pydantic schemas
- Implement tool execution layer for each tool
- Build `AgentLoop` controller in `utils/agent_loop.py`: budget accounting (≤20 tool calls, ≤120s), tool dispatch, termination detection, `AgentLoopResult` output
- Refactor `ha_agent_sandbox_engine.py` and `netalertx/healer.py` to call `AgentLoop.run()`
- Safety audit: `apply_fix` still enforces backup-before-write internally; `run_ha_command` allowlist enforced
- Eval regression check against M5 baseline

**Validation gate:** Score on `evals/run_evals.py` does not drop vs the M5 baseline; `apply_fix` safety audit passes; both HA and NetAlertX healing pipelines use the loop.

Full spec: [plan/tool-loop.md](plan/tool-loop.md)

---

### Milestone 2 — Local RAG & Knowledge Layer

**Objective:** Keep the agent knowledgeable about HA breaking changes and integration updates without live web searches, satisfying the 0 WAN packets constraint.

**Delivered in Phase 15 (after the tool loop).** Originally planned as `[KNOWLEDGE]` block injection into a fixed prompt. Redesigned as a `query_knowledge` tool registered in the Phase 14 tool loop — the agent queries for context only when it judges it useful, avoiding token waste on irrelevant chunks.

**Tasks:**
- Stand up ChromaDB locally on macOS; embed with `nomic-embed-text` via Ollama (zero WAN)
- Weekly scrapers for: HA core release notes (breaking changes section), HACS component changelogs
- `query_knowledge` tool registered in the tool registry; returns top-K ranked chunks with source metadata
- `community_cases` ChromaDB collection created here (empty until Milestone 9 / Phase 19 delivers cases)
- Weekly refresh via macOS `launchd` plist

**Validation gate:** Agent correctly cites a specific HA breaking change from the local vector DB, zero WAN calls.

Full spec: [plan/rag-tool.md](plan/rag-tool.md)

---

### Milestone 5 — Agent Quality & Evaluation

**Objective:** Make regressions visible. Without evals, there is no way to know if a prompt change, model upgrade, or new feature makes the agent better or worse at its actual job. Unit tests verify code correctness; evals verify agent intelligence.

**Delivered in Phase 16 (after the tool loop and RAG layer are in place).** Having both makes the eval scenarios more meaningful — the loop exercises real tool-calling behaviour and RAG provides the knowledge context the agent will have in production.

**Tasks:**
- `evals/scenarios/` — directory of `.yaml` files, each defining: `name`, `input_config` or `input_log_line`, `expected_is_valid`, `expected_severity`, `expected_issue_keywords: list[str]`, `fix_must_parse: bool`
- Minimum 10 scenarios covering: malformed YAML, missing required key, deprecated integration format, valid config (true negative), CRITICAL traceback log line, INFO line (true negative), ambiguous WARNING
- `evals/run_evals.py` — loads each scenario, runs it through the real Ollama inference pipeline, scores results, prints a summary table, saves scores to `evals/baseline.json` on first run, compares against baseline on subsequent runs
- Scoring metrics: `is_valid` accuracy, severity accuracy, issue keyword recall, fix YAML parse success rate, mean inference latency
- `/project:run-evals` slash command — runs `python evals/run_evals.py` and summarises results
- Optional CI job — runs evals against Ollama if available, gated so it does not block PR merges

**Validation gate:** Running `python evals/run_evals.py` produces a score table against ≥ 10 scenarios; a deliberate prompt regression visibly drops the score; baseline is committed and tracked in git.

Full spec: [plan/evals.md](plan/evals.md)

---

### Milestone 7 — Configurable LLM Provider + Cloud Escalation

**Objective:** Make the LLM inference engine a first-class switchable setting so Pueo can run with local Ollama, an Anthropic cloud API, or both. The "0 WAN during autonomous fix cycles" design constraint is explicitly overridden here — cloud mode routes inference traffic to Anthropic. approved escalation (the original M7 goal) becomes the natural behavior of `both` mode: local Ollama handles autonomous repair cycles; when the local loop exhausts its budget the user can approve a Claude escalation from the dashboard.

**Key design choices:**
- `LLM_PROVIDER` setting: `"local"` (default, preserves all existing behavior), `"cloud"` (Anthropic as primary), `"both"` (Ollama for autonomous + Claude for approved escalation)
- `LLMClientProtocol` already exists in `interfaces.py` — `ClaudeAPIClient` implements it without changing the interface or any caller that uses DI
- `make_llm_client()` factory in `utils/llm_factory.py` is the single point that reads `LLM_PROVIDER`; all 20+ `OllamaClient()` fallbacks migrate to it
- `ANTHROPIC_API_KEY` from environment only — never in `config.yaml`; startup raises if provider requires it and it is absent
- Billing caps (`CLOUD_MAX_COST_PER_INCIDENT_USD`, `CLOUD_MAX_DAILY_SPEND_USD`) tracked in a `cloud_spend` SQLite table; caps enforced before each API call
- RAG embeddings always use local Ollama (`nomic-embed-text`) regardless of `LLM_PROVIDER`
- `setup.sh` asks for provider preference and conditionally skips the Ollama inference model pull when `cloud` is chosen

**Tasks:**
- `ClaudeAPIClient` in `utils/cloud_client.py`: tool schema adapter, response normalizer, history translator, structured-output via tool-forcing
- `make_llm_client()` factory + `_default_model_for_provider()` helper in `utils/llm_factory.py`
- Config: `LLM_PROVIDER`, `CLOUD_MODEL`, billing keys; `ANTHROPIC_API_KEY` env guard + credential guard; `config.yaml.default` `llm:` + `cloud:` sections; `setup.sh` provider wizard
- Dashboard `LLM Provider` settings group: provider dropdown (`options`), cloud model text, billing thresholds, API key status badge
- Billing guard: `cloud_spend` DB migration v15; `BillingCapError`; `CARD_TYPE_CLOUD_ESCALATION` approval card; re-run `AgentLoop` with `ClaudeAPIClient` on approval
- ADR 006: LLM provider abstraction

**Validation gate:** `LLM_PROVIDER=local` (default): no cloud SDK touched; `LLM_PROVIDER=cloud`: all call-sites use `ClaudeAPIClient`; `LLM_PROVIDER=both` + loop exhaustion: approval card appears; billing caps block over-budget escalations; `ANTHROPIC_API_KEY` never storable in `config.yaml`.

Full spec: [plan/cloud-escalation.md](plan/cloud-escalation.md)

---

### Milestone 8 — Repair Episode Recording

**Objective:** After every successful repair cycle, serialize a structured `RepairEpisode` to SQLite: symptoms, tool sequence, hypothesis chain, fix applied, outcome, model used. Exportable as anonymized YAML to feed the Federated Case Library.

**Tasks:**
- `repair_episodes` SQLite table (new migration), `RepairEpisode` dataclass, serialization hook at `finish_repair`
- `--mode export-episodes --since <date>` → anonymized YAML (IPs, hostnames, device names replaced with placeholders)
- Episodes tab in dashboard: list, filter, detail view, "Prepare for submission" button

**Validation gate:** Every successful `finish_repair` writes a record; export produces valid anonymized YAML; dashboard tab renders episode detail.

Full spec: [plan/repair-episodes.md](plan/repair-episodes.md)

---

### Milestone 9 — Federated Case Library

**Objective:** Pool anonymized repair episodes in a public `pueo-cases` GitHub repo. Pueo instances contribute (submit PR from dashboard) and consume (weekly pull → vectorize → ChromaDB). Each merged community case also generates an eval scenario, closing the M5 loop.

**Tasks:**
- Case submission: dashboard flow from episode → redacted YAML review → `gh pr create` to `pueo-cases`
- Case ingest: weekly pull of merged cases → embed → upsert into `community_cases` ChromaDB collection
- Eval scenario generation: each ingested case → `.yaml` in `evals/scenarios/community/`

**Validation gate:** One real episode submitted, merged, pulled back, and retrievable via `query_knowledge`; corresponding eval scenario auto-generated and scored by `run_evals.py`.

Full spec: [plan/federated-cases.md](plan/federated-cases.md)

---

### Milestone 10 — Self-Improving Code Proposals  *(stretch goal)*

**Objective:** When Pueo identifies a capability gap during a repair loop, it proposes a Python diff, validates it against CI in a sandboxed temp directory, and surfaces a approval card to open a PR. Approved changes become reusable tools for every future incident.

**Foundation in Milestone 6.6:** The sandbox tools (`read_source`, `propose_patch`, `sandbox_code`) and the `code_proposal` approval card were delivered in Phase 17.5 (Milestone 6.6) as part of the conversational agent's code-skill-building feature. Milestone 10 adds only the remaining pieces: the autonomous trigger and the formal `open_pr` path.

**Remaining tasks (Phase 21, items 83–86):**
- `open_pr` tool: `gh pr create` integration; formal PR opens on approval instead of in-process registration
- Autonomous gap detection: `finish_repair` with `capability_gap=True` automatically triggers `propose_patch → sandbox_code → code_proposal` approval card
- Security review: sandbox escape vectors, safety-critical file block list (`utils/autonomy.py`, `interfaces.py`, `config.py`, backup invariant chain), `read_source` path traversal
- ADR 007: agent-generated code proposals with sandboxed CI gate

**Validation gate:** Agent proposes a new tool for a synthetic gap scenario; sandbox CI runs; approval opens a real PR; safety-critical block list tested; security review complete.

Full spec: [plan/code-proposals.md](plan/code-proposals.md)

---

### Milestone 11 — Transparent Operation

**Objective:** Make Pueo's reasoning visible in real time. Users can see what Pueo has done (event timeline, repair episodes) and what it is currently thinking (live tool-call trace in the Chat tab). Transparency becomes a first-class design goal alongside safety, privacy, and autonomy.

**Why here:** Pueo makes autonomous decisions that affect a live smart home. Users need confidence that they can audit everything that happened and observe reasoning before acting. The Chat tab today shows a collapsed "N tool calls" list after the fact — users cannot follow along or intervene. The event timeline records completion but not per-step progress during autonomous repairs.

**Key design choices:**
- `AgentLoop` gains `pre_step_callback` (fires before each tool execution) alongside the existing `step_callback` (fires after). The pre-call event carries tool name + sanitized arguments preview; the post-call event carries output summary + success flag.
- Chat trace renders as inline expandable cards (spinner → ✓/✗ badge), replacing the collapsed `<details>` element.
- Autonomous repair loops emit `agent_step` SSE events to the general bus so the overview page can show a live "Active repair" widget.
- Chat guidance injection (Chunk 4) allows typing context into Chat while a repair is running; the running loop picks it up on the next LLM call.

**Tasks (four independent chunks):**
- **Chunk 1:** `pre_step_callback` hook in `AgentLoop` + chat pre/post call trace in dashboard + inline trace cards in `chat.html`
- **Chunk 2:** Durable tool context in `chat_messages` history; "Show tool calls" toggle in `chat.html`
- **Chunk 3:** `timeline_callback` in `AgentLoop`; `agent_step` SSE on autonomous repairs; "Active repair" widget on overview; `result_summary` field in repair episode tool sequence
- **Chunk 4 (stretch):** `inject_context()` on running `AgentLoop`; `POST /repair/inject` endpoint; guidance banner in `chat.html`

**Validation gate:**
- Chat: asking "What is the HA disk usage?" shows a spinner card before `run_ha_command` returns, then a ✓ badge with output summary, then the final answer
- History: reloading a session and toggling "Show tool calls" renders tool trace for past turns
- Autonomous repairs: overview shows live step widget; timeline records per-step rows
- CI gate passes on each chunk's PR

Full spec: [plan/transparency.md](plan/transparency.md)

---

## Evaluation Matrix

These constraints govern all ongoing development. Evaluate every new feature against them before merging.

| Constraint | Target | Mitigation if failing |
|---|---|---|
| Inference latency | < 4 seconds per agent step | Quantize model to `q4_K_M`; offload embedding layers to Apple Silicon AMX |
| Config hallucination | Zero on inputs up to 8,000 tokens | Sliding window log ingestion; pass only relevant config sections, not full directories |
| Un-backed writes | 0% — no production write without a confirmed backup slug | `execute_remote_backup()` raises on failure; pipeline aborts |
| LLM inference location | Configurable: `local` (Ollama, default), `cloud` (Anthropic API), or `both` | Set via `LLM_PROVIDER`; cloud and both require `ANTHROPIC_API_KEY` env var; billing caps enforced; WAN only via the designated provider |
| WAN during autonomous fix cycles | 0 when `LLM_PROVIDER=local` (default) | Cloud mode intentionally sends inference traffic to Anthropic; autonomous cycles in `both` mode still use local Ollama |
| HA disk free | ≥ `HA_DISK_CRITICAL_GB` at all times | Block backup trigger + offload older backups automatically before new backup fires. Note: the HA Supervisor independently hard-blocks **all** operations (including `ha backups new`) when free space < 1 GB — Pueo's threshold must remain above `1 GB + largest expected backup size` or the Supervisor will block Pueo's own backup before Pueo's guard can act. Default is 3.0 GB (2 GB above the Supervisor's floor). |
| Backup location | 100% of slugs confirmed on Pueo before deleting from HA | SHA-256 gate; `location = 'both'` required before any HA-side delete |
| Tool loop budget | ≤ 20 tool calls per incident | Hard cap in `AgentLoop`; exhaustion triggers escalation offer, not silent failure |
| Loop wall time | ≤ 120 seconds | `asyncio` timeout wrapping `AgentLoop.run()`; same outcome as budget exhaustion |
| Local fix rate | ≥ 80% resolved without cloud escalation | Tune tool count + model size if falling below; cloud escalation is the fallback |
| Episode coverage | 100% of successful repairs recorded | `finish_repair` tool fires serialization unconditionally |
| Cloud spend | Per-incident cap ($0.50) + daily cap ($5.00) | `BillingCapError` before each API call; tracked in `cloud_spend` SQLite table; caps configurable in UI |

---

## Architectural Note

The original plan specified LangGraph or CrewAI as the agentic framework. Plain `asyncio` was chosen instead — the current state machine is simple enough that a full framework would add dependency weight without benefit. Revisit if the system grows to require multi-agent coordination or complex branching state graphs.
