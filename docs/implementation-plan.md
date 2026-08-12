# Agentic Engineering Practices — Implementation Plan

> **Archived — 2026-08-11.** All items below are complete. New bugs, enhancements, and feature work are tracked in [GitHub Issues](https://github.com/AndysWorth/pueo/issues). This file is kept as a historical record of the initial build-out phases.

Pick up the next incomplete item at the start of a new session: find it in the Status table below, then open the linked detail file for the full specification before writing any code.

## Hierarchy

- **Milestone** (`docs/roadmap.md`) — strategic capability; numbered by capability order (M1–M10 with sub-milestones 4.5/4.6/4.7). Describes *what* is being built and *why*.
- **Phase** — delivery batch of 2–7 items, typically 1–3 sessions. Numbered in **execution order** (not milestone order). Each phase has a body section below and a linked detail file.
- **Item** — atomic PR-sized work unit; numbered sequentially 1–N; the tracking primitive (☐ TODO → ✅ Done in the Status table).

Plan detail files use a **Phase Deliverables** table (item number + one-line description) to link items to spec text. Do not introduce new "Feature N" labels — reference items by number.

Detail files: [plan/foundation.md](plan/foundation.md) · [plan/autonomy.md](plan/autonomy.md) · [plan/netalertx.md](plan/netalertx.md) · [plan/hitl-dashboard.md](plan/hitl-dashboard.md) · [plan/status-logging.md](plan/status-logging.md) · [plan/installer-diagnostics.md](plan/installer-diagnostics.md) · [plan/evidence-trace.md](plan/evidence-trace.md) · [plan/installer-verbose-logging.md](plan/installer-verbose-logging.md) · [plan/netalertx-one-shot-diagnose.md](plan/netalertx-one-shot-diagnose.md) · [plan/mqtt-setup.md](plan/mqtt-setup.md) · [plan/resource-stewardship.md](plan/resource-stewardship.md) · [plan/evals.md](plan/evals.md) · [plan/tool-loop.md](plan/tool-loop.md) · [plan/rag-tool.md](plan/rag-tool.md) · [plan/supervisor.md](plan/supervisor.md) · [plan/conversational-agent.md](plan/conversational-agent.md) · [plan/cloud-escalation.md](plan/cloud-escalation.md) · [plan/repair-episodes.md](plan/repair-episodes.md) · [plan/federated-cases.md](plan/federated-cases.md) · [plan/code-proposals.md](plan/code-proposals.md) · [plan/ha-update-manager.md](plan/ha-update-manager.md) · [plan/ha-notifications.md](plan/ha-notifications.md) · [plan/ha-rag-strategy.md](plan/ha-rag-strategy.md) · [plan/disk-usage.md](plan/disk-usage.md)

---

## Status

| #      | Item                                                                                                                                                                                                                                                                  | Status              |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| 1      | Prompt Management                                                                                                                                                                                                                                                     | ✅ Done (2026-07-15) |
| 2      | Retry with Exponential Backoff                                                                                                                                                                                                                                        | ✅ Done (2026-07-15) |
| 3      | Rate Limiting and Debounce                                                                                                                                                                                                                                            | ✅ Done (2026-07-15) |
| 4      | SQLite Migration Strategy                                                                                                                                                                                                                                             | ✅ Done (2026-07-15) |
| 5      | Structured Logging + Correlation IDs                                                                                                                                                                                                                                  | ✅ Done (2026-07-15) |
| 6      | Context Window / Token Management                                                                                                                                                                                                                                     | ✅ Done (2026-07-15) |
| 7      | Agent Output Content Validation                                                                                                                                                                                                                                       | ✅ Done (2026-07-15) |
| 8      | Dependency Injection / Protocol Interfaces                                                                                                                                                                                                                            | ✅ Done (2026-07-15) |
| 9      | HITL Notification Infrastructure                                                                                                                                                                                                                                      | ✅ Done (2026-07-15) |
| 9.5    | Unified Autonomy Level                                                                                                                                                                                                                                                | ✅ Done (2026-07-19) |
| 10     | NetAlertX Foundation — Package, Config, and API Client                                                                                                                                                                                                                | ✅ Done (2026-07-19) |
| 11     | NetAlertX Installer — Steps 1–4                                                                                                                                                                                                                                       | ✅ Done (2026-07-19) |
| 12     | NetAlertX Installer — Steps 5–8                                                                                                                                                                                                                                       | ✅ Done (2026-07-19) |
| 13     | NetAlertX Device Name Sync — HA Name Reading and Safe Writes                                                                                                                                                                                                          | ✅ Done (2026-07-20) |
| 14     | NetAlertX Device Name Sync — Conflict Resolution and Unknown Devices                                                                                                                                                                                                  | ✅ Done (2026-07-20) |
| 15     | NetAlertX Log Monitoring                                                                                                                                                                                                                                              | ✅ Done (2026-07-20) |
| 16     | NetAlertX Health Polling and MQTT                                                                                                                                                                                                                                     | ✅ Done (2026-07-20) |
| 17     | NetAlertX AI Diagnosis                                                                                                                                                                                                                                                | ✅ Done (2026-07-20) |
| 18     | NetAlertX Autonomy-Gated Healing                                                                                                                                                                                                                                      | ✅ Done (2026-07-20) |
| 19     | NetAlertX HA Integration Maintenance                                                                                                                                                                                                                                  | ✅ Done (2026-07-20) |
| 19.5   | HITL Web Dashboard                                                                                                                                                                                                                                                    | ✅ Done (2026-07-20) |
| 20     | NetAlertX Setup Status Logging                                                                                                                                                                                                                                        | ✅ Done (2026-07-20) |
| 21     | CLI Corrections, NetAlertX Repository Fix, Remove Optionality                                                                                                                                                                                                         | ✅ Done (2026-07-21) |
| 22     | Installer Diagnostic Intelligence                                                                                                                                                                                                                                     | ✅ Done (2026-07-21) |
| 23     | Evidence and LLM Trace Capture                                                                                                                                                                                                                                        | ✅ Done (2026-07-21) |
| 24     | Dashboard Evidence UI                                                                                                                                                                                                                                                 | ✅ Done (2026-07-21) |
| 25     | NetAlertX Old API Migration                                                                                                                                                                                                                                           | ✅ Done (2026-07-21) |
| 26     | Installer Verbose Progress Logging                                                                                                                                                                                                                                    | ✅ Done (2026-07-22) |
| 27     | NetAlertX One-Shot Diagnosis                                                                                                                                                                                                                                          | ✅ Done (2026-07-22) |
| 28     | MQTT Credential Setup                                                                                                                                                                                                                                                 | ✅ Done (2026-07-23) |
| 29     | Disk & Memory Sensing                                                                                                                                                                                                                                                 | ✅ Done (2026-07-24) |
| 30     | Backup Inventory Tracking                                                                                                                                                                                                                                             | ✅ Done (2026-07-24) |
| 31     | Backup Offloading                                                                                                                                                                                                                                                     | ✅ Done (2026-07-24) |
| 32     | Retention Policy & Cleanup                                                                                                                                                                                                                                            | ✅ Done (2026-07-27) |
| 12.5A  | HA Repairs polling: `poll_for_repairs()`; `execute_ha_reboot()`; `ha_repair_history` migration v12; `CARD_TYPE_HA_REPAIR`; dashboard handler                                                                                                                          | ✅ Done (2026-08-05) |
| 12.5B  | Update ordering enforcement: `_update_priority()`; ordering guard in `approve()`; `is_reboot_required_active()` preflight in `execute_update()`                                                                                                                       | ✅ Done (2026-08-05) |
| 33     | HARestClient + HARestClientProtocol; update entity polling; --mode update-check                                                                                                                                                                                       | ✅ Done (2026-07-27) |
| 34     | Breaking change analysis: release notes fetch + cache; UpdateReadinessReport schema                                                                                                                                                                                   | ✅ Done (2026-07-27) |
| 35     | HITL update approval card: per-component, advisory breaking-changes section                                                                                                                                                                                           | ✅ Done (2026-07-27) |
| 36     | Safe update execution (Core, OS, add-ons) + post-update validation                                                                                                                                                                                                    | ✅ Done (2026-07-27) |
| 37     | Pueo self-check after Core update: command catalog smoke-test, LLM cross-reference                                                                                                                                                                                    | ✅ Done (2026-07-27) |
| 38     | Notification polling (persistent_notification.*); NotificationAnalysis schema; notification_history table                                                                                                                                                             | ✅ Done (2026-07-27) |
| 39     | Notification enrichment: reverse DNS, NetAlertX lookup, HA device registry; HAWebSocketClient                                                                                                                                                                         | ✅ Done (2026-07-27) |
| 40     | HITL notification cards + dismissal service call; --mode notifications                                                                                                                                                                                                | ✅ Done (2026-07-27) |
| 41     | Notifications tab in HITL dashboard: pending, history, filters                                                                                                                                                                                                        | ✅ Done (2026-07-27) |
| 42     | Tool registry: ToolDefinition, ToolCall, ToolResult, AgentStep Pydantic schemas                                                                                                                                                                                       | ✅ Done (2026-07-28) |
| 43     | Tool execution: read_config, read_logs, run_ha_command, read_file, query_netalertx, apply_fix, verify_fix, finish_repair                                                                                                                                              | ✅ Done (2026-07-28) |
| 44     | AgentLoop controller: budget accounting, tool dispatch, termination detection                                                                                                                                                                                         | ✅ Done (2026-07-28) |
| 45     | HA sandbox engine refactor: replace linear pipeline with AgentLoop.run()                                                                                                                                                                                              | ✅ Done (2026-07-28) |
| 46     | NetAlertX healer refactor: replace linear pipeline with AgentLoop.run()                                                                                                                                                                                               | ✅ Done (2026-07-28) |
| 47     | Safety audit: apply_fix backup invariant; run_ha_command allowlist; once-per-loop cap                                                                                                                                                                                 | ✅ Done (2026-07-28) |
| 48     | Functional verification: tool call traces for representative HA and NetAlertX repair scenarios; confirm apply_fix enforces backup-first; confirm run_ha_command rejects off-allowlist commands                                                                        | ✅ Done (2026-07-28) |
| 49     | ChromaDB setup + nomic-embed-text embedding; collection schema and client wrapper                                                                                                                                                                                     | ✅ Done (2026-07-28) |
| 50     | HA release notes scraper: fetch, parse breaking-changes sections, chunk, embed, upsert                                                                                                                                                                                | ✅ Done (2026-07-28) |
| 51     | HACS changelog scraper; query_knowledge tool registered in tool registry                                                                                                                                                                                              | ✅ Done (2026-07-28) |
| 52     | Weekly refresh via macOS launchd plist; vector store maintenance                                                                                                                                                                                                      | ✅ Done (2026-07-28) |
| 53     | Evals — scenario library (≥10 YAML files) + run_evals.py + baseline.json                                                                                                                                                                                              | ✅ Done (2026-07-28) |
| 54     | Evals — /project:run-evals slash command + optional CI job                                                                                                                                                                                                            | ✅ Done (2026-07-28) |
| 55     | Supervisor process: asyncio task launcher; LoopSupervisor with health tracking, backoff restart                                                                                                                                                                       | ✅ Done (2026-07-29) |
| 56     | Card-type dispatch: utils/card_types.py; card_type field on all HITL cards; dispatch table in approve()                                                                                                                                                               | ✅ Done (2026-07-29) |
| 57     | Update executor: _execute_queued_update() in dashboard; refactor execute_update() as callable                                                                                                                                                                         | ✅ Done (2026-07-30) |
| 58     | NetAlertX + resource action executors; in-progress spinner state in dashboard                                                                                                                                                                                         | ✅ Done (2026-07-29) |
| 59     | Dashboard home: overview tab with loop health rows, HA state card, resource gauges; SSE /events endpoint                                                                                                                                                              | ✅ Done (2026-07-30) |
| 60     | Live event timeline: timeline_events SQLite table (migration v6); SSE push; drill-down detail view                                                                                                                                                                    | ✅ Done (2026-07-30) |
| 61     | Configuration editor: settings tab; live-apply for runtime params; config.yaml write; restart prompt for connection params                                                                                                                                            | ✅ Done (2026-07-30) |
| 62     | Loop control from dashboard: pause/resume/run-now per loop via POST endpoints                                                                                                                                                                                         | ✅ Done (2026-07-30) |
| 63     | launchd service: plist template; setup.sh install step; dashboard service status + controls                                                                                                                                                                           | ✅ Done (2026-07-30) |
| 64     | --mode audit: Pueo self-diagnostics; structured gap report (actual vs. intended state); saved to audits/                                                                                                                                                              | ✅ Done (2026-07-30) |
| 65     | DB migration v8: agent_memory, chat_sessions, chat_messages tables                                                                                                                                                                                                    | ✅ Done (2026-07-31) |
| 66     | remember + recall tools: ToolDefinitions, ToolExecutor methods, CHAT_MEMORY_TOP_K + CHAT_ALLOW_TOOL_REGISTRATION config keys                                                                                                                                          | ✅ Done (2026-07-31) |
| 67     | build_chat_tool_registry(); finish_chat ToolDefinition; AgentLoop.terminal_tool_name parameter; conversational system prompt                                                                                                                                          | ✅ Done (2026-07-31) |
| 68     | /chat GET route; chat.html template (session list + message thread + input); base.html nav link                                                                                                                                                                       | ✅ Done (2026-07-31) |
| 69     | POST /chat/message + GET /chat/events SSE; asyncio task dispatch; chat_thinking/chat_done/chat_error events                                                                                                                                                           | ✅ Done (2026-07-31) |
| 70     | read_source, propose_patch, sandbox_code tools: ToolDefinitions + ToolExecutor methods; subprocess CI gate; 60s timeout                                                                                                                                               | ✅ Done (2026-07-31) |
| 71     | add_tool registration: migration v9 (registered_tools), ToolExecutor._dynamic_tools, CARD_TYPE_CODE_PROPOSAL, dashboard HITL handler, user_tools/ loader on startup                                                                                                   | ✅ Done (2026-07-31) |
| 72     | Tests: test_chat.py (migrations v8+v9, remember/recall, chat registry, sandbox_code, read_source, add_tool); TestConfigDefaults for two new config keys                                                                                                               | ✅ Done (2026-07-31) |
| 73     | `ClaudeAPIClient` (tool/response/history adapters); `make_llm_client()` factory in `llm_factory.py`; migrate all `OllamaClient()` call-sites                                                                                                                          | ✅ Done (2026-08-07) |
| 74     | `LLM_PROVIDER`, `CLOUD_MODEL`, billing config keys; `ANTHROPIC_API_KEY` env guard; `setup.sh` provider wizard; ADR 006                                                                                                                                                | ✅ Done (2026-08-07) |
| 75     | Dashboard `LLM Provider` settings group; API key status badge; remaining call-site updates                                                                                                                                                                            | ✅ Done (2026-08-07) |
| 76     | `cloud_spend` DB migration v15; `BillingCapError`; `CARD_TYPE_CLOUD_ESCALATION` HITL card; re-run with `ClaudeAPIClient` on approval                                                                                                                                  | ✅ Done (2026-08-07) |
| 77     | repair_episodes SQLite table (migration); RepairEpisode dataclass; serialization helper                                                                                                                                                                               | ✅ Done (2026-08-10) |
| 78     | Serialization hook at finish_repair in AgentLoop; LLMTrace episode reference                                                                                                                                                                                          | ✅ Done (2026-08-10) |
| 79     | --mode export-episodes CLI; anonymized YAML output; episodes tab in dashboard                                                                                                                                                                                         | ✅ Done (2026-08-10) |
| 80     | Case submission: dashboard review flow → gh pr create to pueo-cases                                                                                                                                                                                                   | ✅ Done (2026-08-11)              |
| 81     | Case ingest: weekly pull → embed → upsert into community_cases ChromaDB                                                                                                                                                                                               | ✅ Done (2026-08-11) |
| 82     | Eval scenario generation from each ingested community case                                                                                                                                                                                                            | ✅ Done (2026-08-11) |
| 83     | open_pr tool: gh pr create integration; builds on propose_patch + sandbox_code from item 70; PR body template with diff + test summary + ADR 007 ref                                                                                                                  | ✅ Done (2026-08-11) |
| 84     | Autonomous gap detection: finish_repair with capability_gap=True triggers propose_patch → sandbox_code → code_proposal HITL card automatically                                                                                                                        | ✅ Done (2026-08-11) |
| 85     | Security review: sandbox escape vectors, safety-critical file block list (utils/autonomy.py, interfaces.py, config.py, backup chain), read_source path traversal                                                                                                      | ✅ Done (2026-08-11) |
| 86     | ADR 007: agent-generated code proposals with sandboxed CI gate                                                                                                                                                                                                        | ✅ Done (2026-08-11) |
| 87     | Stub-body fix: beta fallback in `_fetch_github_release_notes`; neutral advisory in `analyze_breaking_changes`; stub sentinel in `fetch_ha_release_notes`                                                                                                              | ✅ Done (2026-08-06) |
| 88     | `utils/ha_blog_scraper.py`: `fetch_blog_post`, `extract_blog_url_from_stub`, `fetch_blog_release_notes`; hooked into `run_rag_refresh`                                                                                                                                | ✅ Done (2026-08-06) |
| 89     | Enriched chunk metadata (`release_type`, `category`, `impacted_integration`); `where` clause in `KnowledgeStore.query`; `integration_filter` in `query_knowledge` tool                                                                                                | ✅ Done (2026-08-06) |
| 90     | `HAEnvironmentProfile` dataclass + `build_environment_profile`; `get_config_entries` WS method; DB migration v14; save/load helpers                                                                                                                                   | ✅ Done (2026-08-06) |
| 91     | Wire profile into supervisor; `get_ha_profile` chat tool; use profile in `analyze_breaking_changes` and `request_update_approval`                                                                                                                                     | ✅ Done (2026-08-06) |
| 92     | Wire `ChromaKnowledgeStore` into `supervisor_main()` so `query_knowledge` is functional in production                                                                                                                                                                 | ✅ Done (2026-08-06) |
| 93     | HACS version metadata; HA docs `is_installed` flag; `release_type` on bulk-fetched chunks; remove empty `community_cases` collection                                                                                                                                  | ✅ Done (2026-08-06) |
| DU-1   | `utils/disk_usage.py`: dataclasses, SSH helpers, `fetch_disk_breakdown()`, cache, `DiskUsagePoller`                                                                                                                                                                   | ✅ Done (2026-08-07) |
| DU-2   | `config.py` key `DISK_USAGE_POLL_INTERVAL_SECONDS` (default 300); `config.yaml.default`                                                                                                                                                                               | ✅ Done (2026-08-07) |
| DU-3   | `web/templates/disk.html`: 4-section layout, disk gauge, mini-bars, refresh button                                                                                                                                                                                    | ✅ Done (2026-08-07) |
| DU-4   | `web/templates/base.html` nav link; `web/dashboard.py` `GET /disk` + `POST /disk/refresh` routes                                                                                                                                                                      | ✅ Done (2026-08-07) |
| DU-5   | `main.py` supervisor registration of `disk_usage_poll` loop                                                                                                                                                                                                           | ✅ Done (2026-08-07) |
| DU-6   | Tests: `test_utils.py` (35 tests), `test_dashboard.py` (5 tests), `test_config.py` (2 tests)                                                                                                                                                                          | ✅ Done (2026-08-07) |
| STOR-1 | `utils/archiver.py`: `ArchiveResult`, `archive_ha_log`, `archive_journal_dump`, `enforce_archive_retention`                                                                                                                                                           | ✅ Done (2026-08-11) |
| STOR-2 | Config keys: `PUEO_ARCHIVE_DIR`, `ARCHIVE_HA_LOG_ENABLED`, `ARCHIVE_JOURNAL_ENABLED`, `PUEO_ARCHIVE_MAX_GB`, `PUEO_LOCAL_MAX_GB`; triple-update; `archives/` added to `setup.sh --clean`                                                                              | ✅ Done (2026-08-11) |
| STOR-3 | Wire archiver into `disk_recovery.py`: archive HA log before truncate; archive journal before vacuum                                                                                                                                                                  | ✅ Done (2026-08-11) |
| STOR-4 | `utils/pueo_storage.py`: `PueoFootprint`, `measure_pueo_footprint`; warns when `PUEO_LOCAL_MAX_GB` exceeded                                                                                                                                                           | ✅ Done (2026-08-11) |
| STOR-5 | Dashboard: "Pueo Local Storage" card in `disk.html`; `GET /disk` includes footprint; `_backup_reconcile_loop` trims archives                                                                                                                                          | ✅ Done (2026-08-11) |
| STOR-6 | Tests: `test_archiver.py` (14 tests), `test_disk_recovery.py` (archiver integration), `test_config.py` (5 new keys)                                                                                                                                                   | ✅ Done (2026-08-11) |

---

## Phases

### Phase 1–3 — Foundation, Observability, Architecture ✅ Complete (2026-07-15)
Items 1–9. → [plan/foundation.md](plan/foundation.md)

### Phase 3.5 — Cross-Cutting: Autonomy Control ✅ Complete (2026-07-19)
Item 9.5. → [plan/autonomy.md](plan/autonomy.md)

### Phase 4 — NetAlertX Integration ✅ Complete (2026-07-20)
Items 10–19. → [plan/netalertx.md](plan/netalertx.md)

### Phase 4.5 — HITL UX ✅ Complete (2026-07-20)
Item 19.5. → [plan/hitl-dashboard.md](plan/hitl-dashboard.md)

### Phase 5 — Observability UX ✅ Complete (2026-07-20)
Item 20. → [plan/status-logging.md](plan/status-logging.md)

### Phase 6 — Installer Intelligence ✅ Complete (2026-07-21)
Items 21–22. → [plan/installer-diagnostics.md](plan/installer-diagnostics.md)

### Phase 7 — Evidence Capture and HITL Display ✅ Complete (2026-07-21)
Items 23–24. → [plan/evidence-trace.md](plan/evidence-trace.md)

### Phase 8 — NetAlertX Compatibility Maintenance ✅ Complete (2026-07-21)
Item 25. → [plan/netalertx.md](plan/netalertx.md)

### Phase 9 — NetAlertX One-Shot Diagnosis ✅ Complete (2026-07-22)
Item 27. → [plan/netalertx-one-shot-diagnose.md](plan/netalertx-one-shot-diagnose.md)

### Phase 11 — Resource Stewardship ✅ Complete (2026-07-27)
Items 28–32. → [plan/resource-stewardship.md](plan/resource-stewardship.md)

### Phase 12 — HA Update Manager ✅ Complete (2026-07-27)
Items 33–37. → [plan/ha-update-manager.md](plan/ha-update-manager.md)

### Phase 12.5 — HA Repairs & Update Orchestration ✅ Complete (2026-08-05)
Items 12.5A–12.5B. → [plan/ha-update-manager.md](plan/ha-update-manager.md)

### Phase 13 — HA Notification Intelligence ✅ Complete (2026-07-27)
Items 38–41. → [plan/ha-notifications.md](plan/ha-notifications.md)

### Phase 14 — Tool-Calling Agent Loop ✅ Complete (2026-07-28)
Items 42–48. → [plan/tool-loop.md](plan/tool-loop.md)

### Phase 15 — RAG Knowledge Layer ✅ Complete (2026-07-28)
Items 49–52. → [plan/rag-tool.md](plan/rag-tool.md)

### Phase 16 — Evals ✅ Complete (2026-07-28)
Items 53–54. → [plan/evals.md](plan/evals.md)

### Phase 17 — Supervisor + Active Dashboard ✅ Complete (2026-07-30)
Items 55–64. → [plan/supervisor.md](plan/supervisor.md)

### Phase 17.5 — Conversational Agent ✅ Complete (2026-07-31)
Items 65–72. → [plan/conversational-agent.md](plan/conversational-agent.md)

### Phase 18 — Configurable LLM Provider + Cloud Escalation ✅ Complete (2026-08-07)
Items 73–76. → [plan/cloud-escalation.md](plan/cloud-escalation.md)

### Phase 19 — Repair Episode Recording ✅ Complete (2026-08-10)
Items 77–79. → [plan/repair-episodes.md](plan/repair-episodes.md)

---

### Phase 20 — Federated Case Library ✅ Complete (2026-08-11)
Items 80–82. Pool anonymized repair episodes in a public `pueo-cases` GitHub repo. Instances contribute (PR from dashboard) and consume (weekly pull → embed → ChromaDB). Each merged case auto-generates an eval scenario, closing the Phase 16 eval loop.

| Items | Concern |
|-------|---------|
| 80 | Case submission flow (dashboard → gh pr create) |
| 81 | Case ingest (weekly pull → embed → community_cases ChromaDB) |
| 82 | Eval scenario generation from ingested cases |

→ [plan/federated-cases.md](plan/federated-cases.md)

---

### Phase 21 — Self-Improving Code Proposals ✅ Complete (2026-08-11)
Items 83–86. The sandbox infrastructure (`read_source`, `propose_patch`, `sandbox_code`, code_proposal HITL card) was delivered in Phase 17.5. This phase adds the autonomous trigger (agent detects a capability gap during a repair loop), the `open_pr` path (formal PR instead of in-process registration), a security review, and ADR 007. Requires Milestones 7 and 9 complete.

| Items | Concern |
| ----- | ------- |
| 83    | open_pr tool: gh pr create; builds on propose_patch + sandbox_code from item 70; PR body template |
| 84    | Autonomous gap detection: finish_repair with capability_gap=True triggers propose_patch → sandbox_code → code_proposal HITL card |
| 85    | Security review: sandbox escape vectors, safety-critical file block list, read_source path traversal |
| 86    | ADR 007: agent-generated code proposals with sandboxed CI gate |

→ [plan/code-proposals.md](plan/code-proposals.md)

---

### Phase 22 — HA RAG Strategy ✅ Complete (2026-08-06)
Items 87–93. → [plan/ha-rag-strategy.md](plan/ha-rag-strategy.md)

### Phase 23 — Disk Usage Tab ✅ Complete (2026-08-07)
Items DU-1–DU-6. → [plan/disk-usage.md](plan/disk-usage.md)

### Phase 24 — Pueo Local Storage Management ✅ Complete (2026-08-11)
Items STOR-1–STOR-6. → [plan/resource-stewardship.md](plan/resource-stewardship.md)

---

### Phase 25 — NetAlertX Platform Management ☐ In Progress
Sessions A–D. Move NetAlertX FA off HA (disk pressure), add Docker/separate-machine installer, disk-space guards, FA-only enforcement, full MQTT topic coverage.

| Session | Concern | Status |
| ------- | ------- | ------ |
| A | Uninstall from HA + FA-only guard + `ha addons`→`ha apps` fix | ☐ TODO |
| B | Separate-machine Docker installer + MQTT routing | ☐ TODO |
| C | Disk space checks + setup.sh improvements + supervisor migration offer | ☐ TODO |
| D | Full MQTT topic coverage + event deduplication | ☐ TODO |

→ [plan/netalertx-platform-management.md](plan/netalertx-platform-management.md)

---

## Tracking

Update the Status column above (`☐ TODO` → `✅ Done (date)`) **and** the matching entry in the linked detail file when an item completes. Add the PR or commit reference as a note in the detail file.
