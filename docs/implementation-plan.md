# Agentic Engineering Practices — Implementation Plan

Pick up the next incomplete item at the start of a new session: find it in the Status table below, then open the linked detail file for the full specification before writing any code.

Detail files: [plan/foundation.md](plan/foundation.md) · [plan/autonomy.md](plan/autonomy.md) · [plan/netalertx.md](plan/netalertx.md) · [plan/hitl-dashboard.md](plan/hitl-dashboard.md) · [plan/status-logging.md](plan/status-logging.md) · [plan/installer-diagnostics.md](plan/installer-diagnostics.md) · [plan/evidence-trace.md](plan/evidence-trace.md) · [plan/installer-verbose-logging.md](plan/installer-verbose-logging.md) · [plan/netalertx-one-shot-diagnose.md](plan/netalertx-one-shot-diagnose.md) · [plan/mqtt-setup.md](plan/mqtt-setup.md) · [plan/resource-stewardship.md](plan/resource-stewardship.md) · [plan/ha-update-manager.md](plan/ha-update-manager.md) · [plan/ha-notifications.md](plan/ha-notifications.md) · [plan/tool-loop.md](plan/tool-loop.md) · [plan/rag-tool.md](plan/rag-tool.md) · [plan/cloud-escalation.md](plan/cloud-escalation.md) · [plan/repair-episodes.md](plan/repair-episodes.md) · [plan/federated-cases.md](plan/federated-cases.md) · [plan/code-proposals.md](plan/code-proposals.md)

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
| 29   | Disk & Memory Sensing: `ha host info` polling, thresholds, HITL alert, `DiskCriticalError` block | ✅ Done (2026-07-24) |
| 30   | Backup Inventory: SQLite migration, new columns, reconcile on startup, `ha backups list` integration | ✅ Done (2026-07-24) |
| 31   | Backup Offloading: SFTP pull, SHA-256 verify, `location` tracking in SQLite | ✅ Done (2026-07-24) |
| 32   | Retention Policy: HA cleanup after offload, local purge, `--mode backup-status`, dashboard tab | ☐ TODO |
| 62   | `HARestClient` + update entity polling; `UpdateStatus` dataclass; `--mode update-check`; monitor-loop periodic check | ☐ TODO |
| 63   | Breaking-change analysis: release notes fetch + cache, `UpdateReadinessReport` Pydantic schema, LLM advisory | ☐ TODO |
| 64   | HITL update approval card: per-component approval, advisory breaking-changes section, disk-free display | ☐ TODO |
| 65   | Safe update execution: backup invariant, `ha core update`, OS update, add-on Supervisor API update, post-update validation | ☐ TODO |
| 66   | Pueo self-check after Core update: command catalog smoke-test, LLM cross-reference against release notes | ☐ TODO |
| 67   | Notification polling + triage: `persistent_notification.*` REST poll, `NotificationAnalysis` schema, `notification_history` SQLite table | ☐ TODO |
| 68   | Notification enrichment: `http_login` IP → reverse DNS + NetAlertX + HA device registry; `invalid_config` config context; `HAWebSocketClient` | ☐ TODO |
| 69   | Notification HITL cards + dismissal: per-notification card, unknown-IP escalation, dismiss service call | ☐ TODO |
| 70   | Notification history dashboard tab: pending, history, detail view, category/severity filters | ☐ TODO |
| 33   | Eval Scenario Bank: 10+ YAML files, `evals/run_evals.py`, `evals/baseline.json` | ☐ TODO |
| 34   | Eval CI Integration: `/project:run-evals` slash command, optional CI job | ☐ TODO |
| 35   | Tool Registry + Pydantic Schemas: `ToolDefinition`, `ToolCall`, `ToolResult`, `AgentStep` | ☐ TODO |
| 36   | Tool Execution Layer: `read_config`, `read_logs`, `run_ha_command`, `read_file`, `query_netalertx`, `apply_fix`, `verify_fix`, `finish_repair` | ☐ TODO |
| 37   | `AgentLoop` Controller: budget accounting, tool dispatch, termination detection, `AgentLoopResult` | ☐ TODO |
| 38   | HA Agent Pipeline Refactor: replace linear pipeline in `ha_agent_sandbox_engine.py` with `AgentLoop.run()` | ☐ TODO |
| 39   | NetAlertX Healer Refactor: replace linear pipeline in `netalertx/healer.py` with `AgentLoop.run()` | ☐ TODO |
| 40   | Safety Audit: backup invariant in `apply_fix`; `run_ha_command` allowlist; `apply_fix` once-per-loop | ☐ TODO |
| 41   | Eval Regression Check: `run_evals.py` score must not drop vs item-33 baseline | ☐ TODO |
| 42   | ChromaDB Setup + `nomic-embed-text` embedding via Ollama; collection schema and client wrapper | ☐ TODO |
| 43   | HA Release Notes Scraper: fetch, parse breaking-changes sections, chunk, embed, upsert | ☐ TODO |
| 44   | HACS Changelog Scraper + `query_knowledge` tool registered in tool registry | ☐ TODO |
| 45   | Weekly Refresh: macOS `launchd` plist; vector store maintenance (prune stale chunks) | ☐ TODO |
| 46   | `ClaudeAPIClient` + tool adapter; `CLOUD_ESCALATION_ENABLED = false` default enforced at startup | ☐ TODO |
| 47   | Escalation HITL Card: cost estimate, tool history summary, approve/reject with budget display | ☐ TODO |
| 48   | Cloud Response Pipeline: Claude tool calls dispatched via Pueo tool execution layer | ☐ TODO |
| 49   | Billing Guard: per-incident cap, daily rolling cap, `cloud_spend` SQLite table, midnight reset | ☐ TODO |
| 50   | `repair_episodes` SQLite Table: migration, `RepairEpisode` dataclass, serialization helper | ☐ TODO |
| 51   | Episode Serialization Hook at `finish_repair`; update `LLMTrace` to include episode reference | ☐ TODO |
| 52   | Export + Dashboard: `--mode export-episodes --since <date>`, anonymization, episodes tab | ☐ TODO |
| 53   | Case Submission: dashboard review → redact → `gh pr create` to `pueo-cases` repo | ☐ TODO |
| 54   | Case Ingest: weekly pull of merged cases → embed → upsert into `community_cases` ChromaDB collection | ☐ TODO |
| 55   | Eval Scenario Generation: each ingested case → `.yaml` in `evals/scenarios/community/` | ☐ TODO |
| 56   | `read_source` + `propose_patch` tools; diff generation prompt *(stretch)* | ☐ TODO |
| 57   | `sandbox_code` tool: subprocess sandbox, no-network isolation, pytest runner, lint gate *(stretch)* | ☐ TODO |
| 58   | Code Proposal HITL Card: diff viewer, test output, approve/reject *(stretch)* | ☐ TODO |
| 59   | `open_pr` tool: `gh pr create` integration, PR body template *(stretch)* | ☐ TODO |
| 60   | Security Review: sandbox escape vectors, safety-critical file block list *(stretch)* | ☐ TODO |
| 61   | ADR 007: Agent-generated code proposals with sandboxed CI gate *(stretch)* | ☐ TODO |

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

### Phase 4.5 — HITL UX (1 session) ✅ Complete
Item 19.5. Eliminates the 60-minute blocking timeout from `AutonomyGate.require_approval()`, converts monitoring loops to fire healing as `asyncio.create_task()`, and adds a local FastAPI web dashboard (`python main.py --mode dashboard`) for approving or rejecting pending repair actions via browser. Adds `fastapi`, `jinja2`, and `uvicorn` dependencies.

→ [plan/hitl-dashboard.md](plan/hitl-dashboard.md)

---

### Phase 5 — Observability UX (1 session)
Item 20. Wires up `setup_logging()` centrally in `main.py` so all modes emit log output, and adds a human-readable plain-text console formatter used by `--mode netalertx-setup`. Currently the installer emits rich structured events at every step but they are silently dropped because no handlers are attached. The file handler always stays JSON; the stderr handler switches to plain text for the setup wizard.

→ [plan/status-logging.md](plan/status-logging.md)

---

---

### Phase 6 — Installer Intelligence (2 sessions)
Items 21–22. Fixes three CLI command bugs found during documentation review (2026-07-21), removes
the NetAlertX enabled/disabled toggle (NetAlertX is always-on), corrects the add-on repository URL,
and adds evidence-first LLM diagnosis to installer failure paths so Pueo can explain what went wrong
and attempt an automated fix rather than silently aborting.

→ [plan/installer-diagnostics.md](plan/installer-diagnostics.md)

---

### Phase 7 — Evidence Capture and HITL Display (2 sessions)
Items 23–24. When Pueo can't fix a problem, all gathered evidence (log snapshots, SSH command output, raw YAML), the structured diagnosis, and the full LLM prompt/response are currently discarded after use. This phase captures them and surfaces them in the web dashboard HITL cards so the user doesn't have to re-gather evidence manually.

| Items | Concern |
|-------|---------|
| 23 | `LLMTrace` dataclass; 6 LLM call sites return `(ParsedModel, LLMTrace)` tuples; HITL payloads enriched with `diagnosis`, `evidence_raw`, and `llm_trace` keys |
| 24 | Dashboard template: 3 new collapsible sections (Evidence, Diagnosis, LLM Interaction); `epoch_to_iso` Jinja2 filter |

→ [plan/evidence-trace.md](plan/evidence-trace.md)

---

### Phase 8 — NetAlertX Compatibility Maintenance (1 session)
Item 25. The NetAlertX old REST API (`/API_OLD` endpoints) is slated for removal in the next NetAlertX release (flagged since v26.5.4, imminent as of v26.7.1). Although the current Pueo codebase already uses the new API endpoints (`/devices`, `/events`, `/health`, `/settings/<key>`, `/graphql`, `/metrics`, `/nettools/trigger-scan`), this item locks in the migration and adds a version-check guard so Pueo warns at startup if a NetAlertX version is detected that removes expected endpoints.

**Scope:** `netalertx/api_client.py` (remove any old-API fallback paths if present), `netalertx/detector.py` (add minimum-version check against `GET /settings/VERSION`), `tests/test_core.py` (new `TestNetAlertXVersionGuard` class).

**Trigger:** Do this item before the next NetAlertX release drops, or when `GET /settings/VERSION` returns a version > v26.7.1 and integration tests start failing.

---

### Phase 9 — NetAlertX One-Shot Diagnosis (1 session)
Item 27. Adds `--mode netalertx-diagnose`: a single proactive pass that checks the current
state of NetAlertX and the HA integration, synthesises an AI diagnosis, and optionally
triggers healing. Fills the gap between the reactive `--mode netalertx` daemon and having no
way to ask "what is wrong right now?" All building blocks exist (health poller, log triage,
config validator, healer); this item wires them together behind a new CLI entry point.

→ [plan/netalertx-one-shot-diagnose.md](plan/netalertx-one-shot-diagnose.md)

---

### Phase 11 — HA Resource Stewardship (3 sessions) ❌ Not started
Items 29–32. Adds disk and memory monitoring for the HA machine via `ha host info`, backup inventory tracking in SQLite (new migration), SFTP-based backup offloading from HA to Pueo's local machine with SHA-256 verification, and a retention policy that keeps HA disk clean. Directly protects the safety invariant — if HA disk fills, `ha backups new` fails and the pipeline aborts. **Start here before Phase 10.**

→ [plan/resource-stewardship.md](plan/resource-stewardship.md)

---

### Phase 10 — HA Update Manager (3–4 sessions) ❌ Not started
Items 62–66. Adds update detection via REST polling of `update.*` entities, LLM-powered advisory breaking-change analysis (fetches release notes from GitHub, caches locally, runs analysis against current config.yaml), HITL approval cards for Core/OS/add-on updates, safe update execution with the backup invariant, and a post-update Pueo self-check that verifies Pueo's own SSH command catalog still works after a Core update. Monitor loop gains a periodic update-availability check. **Requires Phase 11 (disk sensing) before starting.**

| Items | Concern |
|-------|---------|
| 62 | `HARestClientProtocol`, `HARestClient`, `FakeHARestClient`; `UpdateStatus`; `--mode update-check`; monitor-loop polling |
| 63 | Release notes fetch + cache; `UpdateReadinessReport` schema; LLM breaking-change analysis |
| 64 | HITL update approval card; per-component approval; advisory breaking-changes section |
| 65 | Backup → update execution → post-update validation; Core, OS, add-on update paths |
| 66 | Pueo self-check: SSH command smoke-test + LLM cross-reference against release notes |

→ [plan/ha-update-manager.md](plan/ha-update-manager.md)

---

### Phase 10.5 — HA Notification Intelligence (3 sessions) ❌ Not started
Items 67–70. Polls `persistent_notification.*` REST state entities on a configurable interval. Enriches security notifications (failed login `http_login`) with reverse DNS, NetAlertX device name, and HA device registry lookup. Generates LLM plain-English explanations and recommended actions for every notification. Surfaces HITL cards; dismissal calls the HA dismiss service. Adds a Notifications tab to the HITL dashboard. **Can run in parallel with Phase 10; both require `HARestClient` from item 62.**

| Items | Concern |
|-------|---------|
| 67 | `persistent_notification.*` polling; `NotificationAnalysis` schema; `notification_history` SQLite table |
| 68 | IP enrichment (reverse DNS + NetAlertX + HA device registry); `HAWebSocketClient` for device registry |
| 69 | Per-notification HITL cards; unknown-IP escalation; dismiss service call |
| 70 | Notifications tab in dashboard: pending, history, filters |

→ [plan/ha-notifications.md](plan/ha-notifications.md)

---

### Phase 12 — Evals (2 sessions) ❌ Not started
Items 33–34. Builds the eval scenario bank (10+ YAML files), `evals/run_evals.py`, and commits `evals/baseline.json`. Establishes the measurement baseline before any architecture change. **Required before Phase 13**: the tool loop refactor must have a regression signal.

→ [plan/evals.md](plan/evals.md)

---

### Phase 13 — Tool-Calling Agent Loop (6 sessions) ❌ Not started
Items 35–41. Replaces the fixed linear pipeline with an iterative agent loop using Ollama's `tools` API. The model decides which tools to call at each step. Safety invariant unchanged: `apply_fix` enforces backup-before-write internally. `run_ha_command` enforces an explicit allowlist. Eval regression check (item 41) validates no score drop vs Phase 12 baseline. **Requires Phase 12 baseline before starting.**

→ [plan/tool-loop.md](plan/tool-loop.md)

---

### Phase 14 — RAG Knowledge Layer (4 sessions) ❌ Not started
Items 42–45. Delivers Milestone 2, redesigned. Implemented as a `query_knowledge` tool in the Phase 13 tool registry (not prompt injection). Uses ChromaDB + `nomic-embed-text` via Ollama (zero WAN). Scrapes HA release notes and HACS changelogs weekly via macOS `launchd`. Creates the `community_cases` collection (empty until Phase 17).

→ [plan/rag-tool.md](plan/rag-tool.md)

---

### Phase 15 — HITL Cloud Escalation (3 sessions) ❌ Not started
Items 46–49. When the tool loop exhausts its budget without a fix, offers escalation to Claude (Anthropic API). Opt-in (`CLOUD_ESCALATION_ENABLED = false` default), user-approved per-incident, billing-guarded. `ClaudeAPIClient` implements `LLMClientProtocol` and uses the same tool execution layer. `ANTHROPIC_API_KEY` from environment only — never from `config.yaml`.

→ [plan/cloud-escalation.md](plan/cloud-escalation.md)

---

### Phase 16 — Repair Episode Recording (2 sessions) ❌ Not started
Items 50–52. Serializes every successful repair cycle to a `repair_episodes` SQLite table at `finish_repair`. Adds `--mode export-episodes` with anonymization (IPs, hostnames, device names replaced with placeholders) and an episodes tab in the HITL dashboard. Feeds the Federated Case Library (Phase 17).

→ [plan/repair-episodes.md](plan/repair-episodes.md)

---

### Phase 17 — Federated Case Library (3 sessions) ❌ Not started
Items 53–55. Enables contributing anonymized repair episodes to a public `pueo-cases` GitHub repo and consuming merged cases as a RAG vector source in the `community_cases` ChromaDB collection. Each ingested community case auto-generates an eval scenario in `evals/scenarios/community/`, closing the Phase 12 loop.

→ [plan/federated-cases.md](plan/federated-cases.md)

---

### Phase 18 — Self-Improving Code Proposals *(stretch, 6 sessions)* ❌ Not started
Items 56–61. Adds `read_source`, `propose_patch`, `sandbox_code`, and `open_pr` tools. The agent proposes Python diffs for capability gaps, validates them against CI in a subprocess sandbox (no network, 60s timeout), and surfaces a HITL PR approval card. Safety-critical files (`utils/autonomy.py`, `interfaces.py`, `config.py`, backup invariant chain) are blocked from agent modification without an additional confirmation step. Does not block any other phase.

→ [plan/code-proposals.md](plan/code-proposals.md)

---

## Tracking

Update the Status column above (`☐ TODO` → `✅ Done (date)`) **and** the matching entry in the linked detail file when an item completes. Add the PR or commit reference as a note in the detail file.
