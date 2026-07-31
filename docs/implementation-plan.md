# Agentic Engineering Practices — Implementation Plan

Pick up the next incomplete item at the start of a new session: find it in the Status table below, then open the linked detail file for the full specification before writing any code.

## Hierarchy

- **Milestone** (`docs/roadmap.md`) — strategic capability; numbered by capability order (M1–M10 with sub-milestones 4.5/4.6/4.7). Describes *what* is being built and *why*.
- **Phase** — delivery batch of 2–7 items, typically 1–3 sessions. Numbered in **execution order** (not milestone order). Each phase has a body section below and a linked detail file.
- **Item** — atomic PR-sized work unit; numbered sequentially 1–N; the tracking primitive (☐ TODO → ✅ Done in the Status table).

Plan detail files use a **Phase Deliverables** table (item number + one-line description) to link items to spec text. Do not introduce new "Feature N" labels — reference items by number.

Detail files: [plan/foundation.md](plan/foundation.md) · [plan/autonomy.md](plan/autonomy.md) · [plan/netalertx.md](plan/netalertx.md) · [plan/hitl-dashboard.md](plan/hitl-dashboard.md) · [plan/status-logging.md](plan/status-logging.md) · [plan/installer-diagnostics.md](plan/installer-diagnostics.md) · [plan/evidence-trace.md](plan/evidence-trace.md) · [plan/installer-verbose-logging.md](plan/installer-verbose-logging.md) · [plan/netalertx-one-shot-diagnose.md](plan/netalertx-one-shot-diagnose.md) · [plan/mqtt-setup.md](plan/mqtt-setup.md) · [plan/resource-stewardship.md](plan/resource-stewardship.md) · [plan/evals.md](plan/evals.md) · [plan/tool-loop.md](plan/tool-loop.md) · [plan/rag-tool.md](plan/rag-tool.md) · [plan/supervisor.md](plan/supervisor.md) · [plan/conversational-agent.md](plan/conversational-agent.md) · [plan/cloud-escalation.md](plan/cloud-escalation.md) · [plan/repair-episodes.md](plan/repair-episodes.md) · [plan/federated-cases.md](plan/federated-cases.md) · [plan/code-proposals.md](plan/code-proposals.md) · [plan/ha-update-manager.md](plan/ha-update-manager.md) · [plan/ha-notifications.md](plan/ha-notifications.md)

---

## Status

| #    | Item                                                                 | Status              |
| ---- | -------------------------------------------------------------------- | ------------------- |
| 1    | Prompt Management                                                    | ✅ Done (2026-07-15) |
| 2    | Retry with Exponential Backoff                                       | ✅ Done (2026-07-15) |
| 3    | Rate Limiting and Debounce                                           | ✅ Done (2026-07-15) |
| 4    | SQLite Migration Strategy                                            | ✅ Done (2026-07-15) |
| 5    | Structured Logging + Correlation IDs                                 | ✅ Done (2026-07-15) |
| 6    | Context Window / Token Management                                    | ✅ Done (2026-07-15) |
| 7    | Agent Output Content Validation                                      | ✅ Done (2026-07-15) |
| 8    | Dependency Injection / Protocol Interfaces                           | ✅ Done (2026-07-15) |
| 9    | HITL Notification Infrastructure                                     | ✅ Done (2026-07-15) |
| 9.5  | Unified Autonomy Level                                               | ✅ Done (2026-07-19) |
| 10   | NetAlertX Foundation — Package, Config, and API Client               | ✅ Done (2026-07-19) |
| 11   | NetAlertX Installer — Steps 1–4                                      | ✅ Done (2026-07-19) |
| 12   | NetAlertX Installer — Steps 5–8                                      | ✅ Done (2026-07-19) |
| 13   | NetAlertX Device Name Sync — HA Name Reading and Safe Writes         | ✅ Done (2026-07-20) |
| 14   | NetAlertX Device Name Sync — Conflict Resolution and Unknown Devices | ✅ Done (2026-07-20) |
| 15   | NetAlertX Log Monitoring                                             | ✅ Done (2026-07-20) |
| 16   | NetAlertX Health Polling and MQTT                                    | ✅ Done (2026-07-20) |
| 17   | NetAlertX AI Diagnosis                                               | ✅ Done (2026-07-20) |
| 18   | NetAlertX Autonomy-Gated Healing                                     | ✅ Done (2026-07-20) |
| 19   | NetAlertX HA Integration Maintenance                                 | ✅ Done (2026-07-20) |
| 19.5 | HITL Web Dashboard                                                   | ✅ Done (2026-07-20) |
| 20   | NetAlertX Setup Status Logging                                       | ✅ Done (2026-07-20) |
| 21   | CLI Corrections, NetAlertX Repository Fix, Remove Optionality        | ✅ Done (2026-07-21) |
| 22   | Installer Diagnostic Intelligence                                    | ✅ Done (2026-07-21) |
| 23   | Evidence and LLM Trace Capture                                       | ✅ Done (2026-07-21) |
| 24   | Dashboard Evidence UI                                                | ✅ Done (2026-07-21) |
| 25   | NetAlertX Old API Migration                                          | ✅ Done (2026-07-21) |
| 26   | Installer Verbose Progress Logging                                   | ✅ Done (2026-07-22) |
| 27   | NetAlertX One-Shot Diagnosis                                         | ✅ Done (2026-07-22) |
| 28   | MQTT Credential Setup                                                | ✅ Done (2026-07-23) |
| 29   | Disk & Memory Sensing                                                | ✅ Done (2026-07-24) |
| 30   | Backup Inventory Tracking                                            | ✅ Done (2026-07-24) |
| 31   | Backup Offloading                                                    | ✅ Done (2026-07-24) |
| 32   | Retention Policy & Cleanup                                           | ✅ Done (2026-07-27) |
| 33   | HARestClient + HARestClientProtocol; update entity polling; --mode update-check | ✅ Done (2026-07-27) |
| 34   | Breaking change analysis: release notes fetch + cache; UpdateReadinessReport schema | ✅ Done (2026-07-27) |
| 35   | HITL update approval card: per-component, advisory breaking-changes section | ✅ Done (2026-07-27) |
| 36   | Safe update execution (Core, OS, add-ons) + post-update validation   | ✅ Done (2026-07-27) |
| 37   | Pueo self-check after Core update: command catalog smoke-test, LLM cross-reference | ✅ Done (2026-07-27) |
| 38   | Notification polling (persistent_notification.*); NotificationAnalysis schema; notification_history table | ✅ Done (2026-07-27) |
| 39   | Notification enrichment: reverse DNS, NetAlertX lookup, HA device registry; HAWebSocketClient | ✅ Done (2026-07-27) |
| 40   | HITL notification cards + dismissal service call; --mode notifications | ✅ Done (2026-07-27) |
| 41   | Notifications tab in HITL dashboard: pending, history, filters       | ✅ Done (2026-07-27) |
| 42   | Tool registry: ToolDefinition, ToolCall, ToolResult, AgentStep Pydantic schemas | ✅ Done (2026-07-28) |
| 43   | Tool execution: read_config, read_logs, run_ha_command, read_file, query_netalertx, apply_fix, verify_fix, finish_repair | ✅ Done (2026-07-28) |
| 44   | AgentLoop controller: budget accounting, tool dispatch, termination detection | ✅ Done (2026-07-28) |
| 45   | HA sandbox engine refactor: replace linear pipeline with AgentLoop.run() | ✅ Done (2026-07-28) |
| 46   | NetAlertX healer refactor: replace linear pipeline with AgentLoop.run() | ✅ Done (2026-07-28) |
| 47   | Safety audit: apply_fix backup invariant; run_ha_command allowlist; once-per-loop cap | ✅ Done (2026-07-28) |
| 48   | Functional verification: tool call traces for representative HA and NetAlertX repair scenarios; confirm apply_fix enforces backup-first; confirm run_ha_command rejects off-allowlist commands | ✅ Done (2026-07-28) |
| 49   | ChromaDB setup + nomic-embed-text embedding; collection schema and client wrapper | ✅ Done (2026-07-28) |
| 50   | HA release notes scraper: fetch, parse breaking-changes sections, chunk, embed, upsert | ✅ Done (2026-07-28) |
| 51   | HACS changelog scraper; query_knowledge tool registered in tool registry | ✅ Done (2026-07-28) |
| 52   | Weekly refresh via macOS launchd plist; vector store maintenance     | ✅ Done (2026-07-28) |
| 53   | Evals — scenario library (≥10 YAML files) + run_evals.py + baseline.json | ✅ Done (2026-07-28) |
| 54   | Evals — /project:run-evals slash command + optional CI job           | ✅ Done (2026-07-28) |
| 55   | Supervisor process: asyncio task launcher; LoopSupervisor with health tracking, backoff restart | ✅ Done (2026-07-29) |
| 56   | Card-type dispatch: utils/card_types.py; card_type field on all HITL cards; dispatch table in approve() | ✅ Done (2026-07-29) |
| 57   | Update executor: _execute_queued_update() in dashboard; refactor execute_update() as callable | ✅ Done (2026-07-30) |
| 58   | NetAlertX + resource action executors; in-progress spinner state in dashboard | ✅ Done (2026-07-29) |
| 59   | Dashboard home: overview tab with loop health rows, HA state card, resource gauges; SSE /events endpoint | ✅ Done (2026-07-30) |
| 60   | Live event timeline: timeline_events SQLite table (migration v6); SSE push; drill-down detail view | ✅ Done (2026-07-30) |
| 61   | Configuration editor: settings tab; live-apply for runtime params; config.yaml write; restart prompt for connection params | ✅ Done (2026-07-30) |
| 62   | Loop control from dashboard: pause/resume/run-now per loop via POST endpoints | ✅ Done (2026-07-30) |
| 63   | launchd service: plist template; setup.sh install step; dashboard service status + controls | ✅ Done (2026-07-30) |
| 64   | --mode audit: Pueo self-diagnostics; structured gap report (actual vs. intended state); saved to audits/ | ✅ Done (2026-07-30) |
| 65   | DB migration v8: agent_memory, chat_sessions, chat_messages tables   | ✅ Done (2026-07-31) |
| 66   | remember + recall tools: ToolDefinitions, ToolExecutor methods, CHAT_MEMORY_TOP_K + CHAT_ALLOW_TOOL_REGISTRATION config keys | ✅ Done (2026-07-31) |
| 67   | build_chat_tool_registry(); finish_chat ToolDefinition; AgentLoop.terminal_tool_name parameter; conversational system prompt | ☐ TODO |
| 68   | /chat GET route; chat.html template (session list + message thread + input); base.html nav link | ☐ TODO |
| 69   | POST /chat/message + GET /chat/events SSE; asyncio task dispatch; chat_thinking/chat_done/chat_error events | ☐ TODO |
| 70   | read_source, propose_patch, sandbox_code tools: ToolDefinitions + ToolExecutor methods; subprocess CI gate; 60s timeout | ☐ TODO |
| 71   | add_tool registration: migration v9 (registered_tools), ToolExecutor._dynamic_tools, CARD_TYPE_CODE_PROPOSAL, dashboard HITL handler, user_tools/ loader on startup | ☐ TODO |
| 72   | Tests: test_chat.py (migrations v8+v9, remember/recall, chat registry, sandbox_code, read_source, add_tool); TestConfigDefaults for two new config keys | ☐ TODO |
| 73   | ClaudeAPIClient + tool adapter; CLOUD_ESCALATION_ENABLED = false default | ☐ TODO |
| 74   | Escalation HITL card: cost estimate, tool history summary, approve/reject | ☐ TODO |
| 75   | Cloud response pipeline: Claude tool calls dispatched via Pueo tool execution layer | ☐ TODO |
| 76   | Billing guard: per-incident cap, daily cap, cloud_spend SQLite table | ☐ TODO |
| 77   | repair_episodes SQLite table (migration); RepairEpisode dataclass; serialization helper | ☐ TODO |
| 78   | Serialization hook at finish_repair in AgentLoop; LLMTrace episode reference | ☐ TODO |
| 79   | --mode export-episodes CLI; anonymized YAML output; episodes tab in dashboard | ☐ TODO |
| 80   | Case submission: dashboard review flow → gh pr create to pueo-cases  | ☐ TODO |
| 81   | Case ingest: weekly pull → embed → upsert into community_cases ChromaDB | ☐ TODO |
| 82   | Eval scenario generation from each ingested community case           | ☐ TODO |
| 83   | open_pr tool: gh pr create integration; builds on propose_patch + sandbox_code from item 70; PR body template with diff + test summary + ADR 007 ref | ☐ TODO |
| 84   | Autonomous gap detection: finish_repair with capability_gap=True triggers propose_patch → sandbox_code → code_proposal HITL card automatically | ☐ TODO |
| 85   | Security review: sandbox escape vectors, safety-critical file block list (utils/autonomy.py, interfaces.py, config.py, backup chain), read_source path traversal | ☐ TODO |
| 86   | ADR 007: agent-generated code proposals with sandboxed CI gate       | ☐ TODO |

---

## Phases

### Phase 1–3 — Foundation, Observability, Architecture ✅ Complete
Items 1–9. All complete as of 2026-07-15. Covers prompt management, SSH/Ollama retry with backoff, rate limiting and debounce, SQLite migration versioning, structured JSON logging with correlation IDs, token budget management, YAML content validation, dependency injection via Protocol interfaces, and HITL notification infrastructure (FileNotifier, NtfyNotifier, WebhookNotifier).

→ [plan/foundation.md](plan/foundation.md)

---

### Phase 3.5 — Cross-Cutting: Autonomy Control (1 session) ✅ Complete (2026-07-19)
Item 9.5. Adds `agent.autonomy_level` (integer 1–4, default 2) and `AutonomyGate` — the single ask/skip decision point imported by every Pueo module. Also adds `FakeAutonomyGate` for tests. Refactors the hardcoded `requires_hitl()` in the HA sandbox engine. **All Phase 4 items depend on this being implemented first.**

Levels: 1 = report only · 2 = suggest + approve all · 3 = auto LOW-risk + approve MEDIUM/HIGH/CRITICAL · 4 = auto LOW/MEDIUM/HIGH + approve CRITICAL only.

→ [plan/autonomy.md](plan/autonomy.md)

---

### Phase 4 — NetAlertX Integration (11–14 sessions) ✅ Complete (2026-07-20)
Items 10–19. Full lifecycle for a new integration target: install from scratch (items 10–12), sync device names from HA (13–14), monitor logs and health (15–16), AI diagnosis (17), autonomy-gated healing (18), and ongoing HA integration maintenance (19). Requires Phase 3.5 complete before item 10.

| Items | Concern |
|-------|---------|
| 10 | Package skeleton, all config keys, SQLite migration, detector, API client |
| 11–12 | Idempotent installer: 8-step state machine across two sessions |
| 13–14 | HA→NetAlertX device name sync across two sessions |
| 15–16 | Continuous monitoring: log tail and health polling/MQTT |
| 17 | AI diagnosis prompts, Pydantic schema, config validator |
| 18–19 | Healing actions gated by autonomy level; HA integration maintenance |

→ [plan/netalertx.md](plan/netalertx.md)

---

### Phase 4.5 — HITL UX (1 session) ✅ Complete (2026-07-20)
Item 19.5. Eliminates the 60-minute blocking timeout from `AutonomyGate.require_approval()`, converts monitoring loops to fire healing as `asyncio.create_task()`, and adds a local FastAPI web dashboard (`python main.py --mode dashboard`) for approving or rejecting pending repair actions via browser. Adds `fastapi`, `jinja2`, and `uvicorn` dependencies.

→ [plan/hitl-dashboard.md](plan/hitl-dashboard.md)

---

### Phase 5 — Observability UX (1 session) ✅ Complete (2026-07-20)
Item 20. Wires up `setup_logging()` centrally in `main.py` so all modes emit log output, and adds a human-readable plain-text console formatter used by `--mode netalertx-setup`. Currently the installer emits rich structured events at every step but they are silently dropped because no handlers are attached. The file handler always stays JSON; the stderr handler switches to plain text for the setup wizard.

→ [plan/status-logging.md](plan/status-logging.md)

---

---

### Phase 6 — Installer Intelligence (2 sessions) ✅ Complete (2026-07-21)
Items 21–22. Fixes three CLI command bugs found during documentation review (2026-07-21), removes
the NetAlertX enabled/disabled toggle (NetAlertX is always-on), corrects the add-on repository URL,
and adds evidence-first LLM diagnosis to installer failure paths so Pueo can explain what went wrong
and attempt an automated fix rather than silently aborting.

→ [plan/installer-diagnostics.md](plan/installer-diagnostics.md)

---

### Phase 7 — Evidence Capture and HITL Display (2 sessions) ✅ Complete (2026-07-21)
Items 23–24. When Pueo can't fix a problem, all gathered evidence (log snapshots, SSH command output, raw YAML), the structured diagnosis, and the full LLM prompt/response are currently discarded after use. This phase captures them and surfaces them in the web dashboard HITL cards so the user doesn't have to re-gather evidence manually.

| Items | Concern |
|-------|---------|
| 23 | `LLMTrace` dataclass; 6 LLM call sites return `(ParsedModel, LLMTrace)` tuples; HITL payloads enriched with `diagnosis`, `evidence_raw`, and `llm_trace` keys |
| 24 | Dashboard template: 3 new collapsible sections (Evidence, Diagnosis, LLM Interaction); `epoch_to_iso` Jinja2 filter |

→ [plan/evidence-trace.md](plan/evidence-trace.md)

---

### Phase 8 — NetAlertX Compatibility Maintenance (1 session) ✅ Complete (2026-07-21)
Item 25. The NetAlertX old REST API (`/API_OLD` endpoints) is slated for removal in the next NetAlertX release (flagged since v26.5.4, imminent as of v26.7.1). Although the current Pueo codebase already uses the new API endpoints (`/devices`, `/events`, `/health`, `/settings/<key>`, `/graphql`, `/metrics`, `/nettools/trigger-scan`), this item locks in the migration and adds a version-check guard so Pueo warns at startup if a NetAlertX version is detected that removes expected endpoints.

**Scope:** `netalertx/api_client.py` (remove any old-API fallback paths if present), `netalertx/detector.py` (add minimum-version check against `GET /settings/VERSION`), `tests/test_core.py` (new `TestNetAlertXVersionGuard` class).

**Trigger:** Do this item before the next NetAlertX release drops, or when `GET /settings/VERSION` returns a version > v26.7.1 and integration tests start failing.

---

### Phase 9 — NetAlertX One-Shot Diagnosis (1 session) ✅ Complete (2026-07-22)
Item 27. Adds `--mode netalertx-diagnose`: a single proactive pass that checks the current
state of NetAlertX and the HA integration, synthesises an AI diagnosis, and optionally
triggers healing. Fills the gap between the reactive `--mode netalertx` daemon and having no
way to ask "what is wrong right now?" All building blocks exist (health poller, log triage,
config validator, healer); this item wires them together behind a new CLI entry point.

→ [plan/netalertx-one-shot-diagnose.md](plan/netalertx-one-shot-diagnose.md)

---

> **Note:** Phase 10 is intentionally retired (gap between Phase 9 and Phase 11 is permanent).

### Phase 11 — Resource Stewardship (4 items) ✅ Complete (2026-07-27)
Items 28–32. Protects the backup-before-write safety invariant by keeping HA disk free.

| Item | Concern |
|------|---------|
| 28 | MQTT credential setup |
| 29 | Disk & memory sensing (`ha host info` polling, HITL alerts, `DiskCriticalError` block) |
| 30 | Backup inventory tracking (SQLite migration v5, reconcile on startup) |
| 31 | Backup offloading (SFTP pull, SHA-256 verify, `location='both'`) |
| 32 | Retention policy (`enforce_ha_retention`, `purge_local_backups`, `--mode backup-status`, dashboard `/backups` tab) |

→ [plan/resource-stewardship.md](plan/resource-stewardship.md)

---

---

### Phase 12 — HA Update Manager (5 items) ✅ Complete (2026-07-27)
Items 33–37. Detects available Core, OS, and add-on updates via `update.*` REST entities; evaluates each update for breaking changes using local LLM analysis of cached release notes; executes updates with the backup invariant intact; validates Pueo's own command catalog survives a Core update. Prerequisite: Phase 11 complete (`execute_remote_backup()` blocks on `DiskCriticalError`).

| Items | Concern |
|-------|---------|
| 33 | HARestClient + update entity polling + --mode update-check |
| 34 | Breaking change analysis: release notes fetch + cache + UpdateReadinessReport |
| 35 | HITL update approval card (CRITICAL for Core/OS; MEDIUM for add-ons) |
| 36 | Safe update execution (Core, OS, add-ons) + post-update log triage |
| 37 | Pueo self-check after Core update: command catalog smoke-test + LLM cross-reference |

→ [plan/ha-update-manager.md](plan/ha-update-manager.md)

---

### Phase 13 — HA Notification Intelligence (4 items) ✅ Complete (2026-07-27)
Items 38–41. Surfaces HA persistent notifications (`persistent_notification.*`) as enriched HITL cards with plain-English explanations, IP device enrichment, and recommended actions. Uses `HARestClient` from item 33. `http_login` notifications enriched via reverse DNS + NetAlertX + HA device registry; unknown-source logins escalated to CRITICAL.

| Items | Concern |
|-------|---------|
| 38 | Notification polling + NotificationAnalysis schema + notification_history table |
| 39 | Notification enrichment: reverse DNS, NetAlertX, HA device registry via HAWebSocketClient |
| 40 | HITL notification cards + dismissal + --mode notifications |
| 41 | Notifications tab in HITL dashboard: pending, history, filters |

→ [plan/ha-notifications.md](plan/ha-notifications.md)

---

### Phase 14 — Tool-Calling Agent Loop (7 items) ✅ Complete (2026-07-28)
Items 42–48. Replaces the linear pipeline with an iterative agent loop using Ollama's `tools` API. The model decides which tools to call, iterates until it fixes the problem or exhausts a budget (≤20 tool calls, ≤120s). Both HA and NetAlertX healing pipelines are refactored to use `AgentLoop.run()`. Phase 16 (Evals) follows and establishes the tool loop's first performance baseline.

| Items | Concern |
|-------|---------|
| 42–43 | Tool registry Pydantic schemas + execution implementations |
| 44 | AgentLoop controller |
| 45–46 | HA sandbox engine + NetAlertX healer refactors |
| 47 | Safety audit |
| 48 | Functional verification: tool call traces for representative HA and NetAlertX repair scenarios |

→ [plan/tool-loop.md](plan/tool-loop.md)

---

### Phase 15 — RAG Knowledge Layer (4 items) ✅ Complete (2026-07-28)
Items 49–52. Keeps the agent knowledgeable about HA breaking changes without WAN calls during fix cycles. ChromaDB + `nomic-embed-text` via Ollama; weekly scrapers for HA release notes and HACS changelogs; `query_knowledge` tool registered in the loop (slot reserved from item 42). Requires Phase 14 complete.

| Items | Concern |
|-------|---------|
| 49 | ChromaDB setup + embedding client wrapper |
| 50 | HA release notes scraper |
| 51 | HACS changelog scraper + query_knowledge tool |
| 52 | Weekly launchd refresh + vector store maintenance |

→ [plan/rag-tool.md](plan/rag-tool.md)

---

### Phase 16 — Evals (2 items) ✅ Complete (2026-07-28)
Items 53–54. Makes regressions visible: unit tests verify code correctness, evals verify agent intelligence. Synthetic YAML scenario library run through the real Ollama pipeline; scored and baselined in git.

| Items | Concern |
|-------|---------|
| 53 | Scenario library (≥10 YAML files) + run_evals.py + baseline.json scoring |
| 54 | /project:run-evals slash command + optional gated CI job |

→ [plan/evals.md](plan/evals.md)

---

### Phase 17 — Supervisor + Active Dashboard (10 items) ✅ Complete (2026-07-30)
Items 55–64. Replaces the collection of disconnected one-shot and daemon commands with a
single `python main.py` supervisor process. All monitoring loops run as asyncio tasks
alongside the FastAPI dashboard. The dashboard becomes the face of Pueo: real-time status
overview, live event timeline, configuration editor, loop control, and direct execution of
HITL-approved actions. A launchd plist keeps Pueo alive across reboots. Prerequisite:
Phase 16 (Evals) complete.

| Items | Concern                                                                                           |
| ----- | ------------------------------------------------------------------------------------------------- |
| 55    | Supervisor process: LoopSupervisor with asyncio task launcher, health tracking, backoff restart   |
| 56    | Card-type dispatch: utils/card_types.py; card_type on all HITL cards; dispatch table in approve() |
| 57    | Update executor in dashboard; refactor execute_update() as standalone callable                    |
| 58    | NetAlertX + resource action executors; in-progress spinner state                                  |
| 59    | Dashboard overview tab: loop health rows, HA state card, resource gauges; SSE /events             |
| 60    | Live event timeline: timeline_events DB table (migration v6); drill-down detail view              |
| 61    | Configuration editor: settings tab; live-apply runtime params; write config.yaml                  |
| 62    | Loop control from dashboard: pause/resume/run-now per loop                                        |
| 63    | launchd service: plist template; setup.sh install step; dashboard service controls                |
| 64    | --mode audit: Pueo self-diagnostics report saved to audits/                                       |

→ [plan/supervisor.md](plan/supervisor.md)

---

### Phase 17.5 — Conversational Agent (8 items)
Items 65–72. Adds a Chat tab to the HITL dashboard, persistent agent memory (SQLite), and an interactive code-sandbox flow for proposing and registering new tools. The same `AgentLoop` that drives repair sessions drives the conversational agent — with a different system prompt, a chat-specific tool registry, and `terminal_tool_name="finish_chat"`. Also implements the shared sandbox infrastructure (`read_source`, `propose_patch`, `sandbox_code`) that Phase 21 will reuse for its autonomous code-proposal path. Prerequisite: Phase 17 complete.

| Items | Concern |
|-------|---------|
| 65 | DB migration v8: agent_memory, chat_sessions, chat_messages |
| 66 | remember + recall tools; CHAT_MEMORY_TOP_K + CHAT_ALLOW_TOOL_REGISTRATION config keys |
| 67 | build_chat_tool_registry(); finish_chat tool; AgentLoop.terminal_tool_name; conversational system prompt |
| 68 | /chat UI: route, chat.html template, base.html nav link |
| 69 | POST /chat/message + GET /chat/events SSE; asyncio task dispatch |
| 70 | read_source, propose_patch, sandbox_code tools; subprocess CI gate |
| 71 | add_tool: migration v9, _dynamic_tools, CARD_TYPE_CODE_PROPOSAL, dashboard HITL handler |
| 72 | Tests: test_chat.py + TestConfigDefaults entries |

→ [plan/conversational-agent.md](plan/conversational-agent.md)

---

### Phase 18 — HITL Cloud Escalation (4 items)
Items 73–76. When the local loop exhausts its budget, offer to escalate to Claude (Anthropic API) — user-approved, per-incident. Same tool registry; full failed-loop history passed as context. Billing guards enforced. `CLOUD_ESCALATION_ENABLED = false` default; `ANTHROPIC_API_KEY` from environment only.

| Items | Concern |
|-------|---------|
| 73 | ClaudeAPIClient + tool adapter |
| 74 | Escalation HITL card |
| 75 | Cloud response pipeline via shared tool execution layer |
| 76 | Billing guard + cloud_spend SQLite table |

→ [plan/cloud-escalation.md](plan/cloud-escalation.md)

---

### Phase 19 — Repair Episode Recording (3 items)
Items 77–79. Every successful repair cycle serializes a structured `RepairEpisode` to SQLite: symptoms, tool sequence, hypothesis chain, fix applied, outcome, model used. Exportable as anonymized YAML. Episodes feed Phase 20 (Federated Case Library).

| Items | Concern |
|-------|---------|
| 77 | repair_episodes migration + RepairEpisode dataclass |
| 78 | Serialization hook at finish_repair in AgentLoop |
| 79 | --mode export-episodes CLI + episodes dashboard tab |

→ [plan/repair-episodes.md](plan/repair-episodes.md)

---

### Phase 20 — Federated Case Library (3 items)
Items 80–82. Pool anonymized repair episodes in a public `pueo-cases` GitHub repo. Instances contribute (PR from dashboard) and consume (weekly pull → embed → ChromaDB). Each merged case auto-generates an eval scenario, closing the Phase 16 eval loop.

| Items | Concern |
|-------|---------|
| 80 | Case submission flow (dashboard → gh pr create) |
| 81 | Case ingest (weekly pull → embed → community_cases ChromaDB) |
| 82 | Eval scenario generation from ingested cases |

→ [plan/federated-cases.md](plan/federated-cases.md)

---

### Phase 21 — Self-Improving Code Proposals *(stretch goal, 4 items)*
Items 83–86. The sandbox infrastructure (`read_source`, `propose_patch`, `sandbox_code`, code_proposal HITL card) was delivered in Phase 17.5. This phase adds the autonomous trigger (agent detects a capability gap during a repair loop), the `open_pr` path (formal PR instead of in-process registration), a security review, and ADR 007. Requires Milestones 7 and 9 complete.

| Items | Concern |
| ----- | ------- |
| 83    | open_pr tool: gh pr create; builds on propose_patch + sandbox_code from item 70; PR body template |
| 84    | Autonomous gap detection: finish_repair with capability_gap=True triggers propose_patch → sandbox_code → code_proposal HITL card |
| 85    | Security review: sandbox escape vectors, safety-critical file block list, read_source path traversal |
| 86    | ADR 007: agent-generated code proposals with sandboxed CI gate |

→ [plan/code-proposals.md](plan/code-proposals.md)

---

## Tracking

Update the Status column above (`☐ TODO` → `✅ Done (date)`) **and** the matching entry in the linked detail file when an item completes. Add the PR or commit reference as a note in the detail file.
