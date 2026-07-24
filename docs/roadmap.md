# Pueo — Development Roadmap

## Milestone Status

The strategic milestones (numbered rows) reflect long-running objectives. Implementation plan phases (lettered rows) are the tactical backlog that delivers them.

| Milestone                                      | Status                  | Module                                   |
| ---------------------------------------------- | ----------------------- | ---------------------------------------- |
| 1. Read-only ingestion & diagnostics           | ✅ Complete              | `ha_agent_core.py`                       |
| 2. Local RAG & knowledge ingestion             | ❌ Not started           | —                                        |
| 3. Safe execution / shadow mode                | ✅ Complete              | `ha_agent_sandbox_engine.py`             |
| 4. Closed-loop autonomous healing              | ✅ Complete              | `ha_agent_sandbox_engine.py`             |
| — Phase 3.5: Autonomy control                  | ✅ Complete (2026-07-19) | `utils/autonomy.py`                      |
| — Phase 4: NetAlertX integration               | ✅ Complete (2026-07-20) | `netalertx/`                             |
| — Phase 4.5: HITL web dashboard                | ✅ Complete (2026-07-20) | `web/dashboard.py`                       |
| — Phase 5: Observability UX                    | ✅ Complete (2026-07-20) | `utils/logging.py`, `main.py`            |
| — Phase 6: Installer Intelligence              | ✅ Complete (2026-07-21) | `netalertx/installer_diagnostics.py`     |
| — Phase 7: Evidence Capture & HITL Display     | ✅ Complete (2026-07-21) | `utils/llm_trace.py`, `web/dashboard.py` |
| — Phase 8: NetAlertX Compatibility Maintenance | ✅ Complete (2026-07-21) | `netalertx/detector.py`                  |
| 5. Agent quality & evaluation                  | ❌ Not started           | `evals/`                                 |
| 4.5. HA Resource Stewardship                   | ❌ Not started           | `ha_agent_advanced.py`, `web/dashboard.py` |
| 6. Tool-calling agent loop                     | ❌ Not started           | `utils/agent_loop.py`                    |
| 7. HITL cloud escalation                       | ❌ Not started           | `utils/cloud_client.py`                  |
| 8. Repair episode recording                    | ❌ Not started           | `ha_agent_advanced.py`                   |
| 9. Federated case library                      | ❌ Not started           | `rag/`                                   |
| 10. Self-improving code proposals *(stretch)*  | ❌ Not started           | —                                        |
| — Phase 11: Resource Stewardship               | ❌ Not started           | items 29–32                              |
| — Phase 12: Evals                              | ❌ Not started           | items 33–34                              |
| — Phase 13: Tool-Calling Agent Loop            | ❌ Not started           | items 35–41                              |
| — Phase 14: RAG Knowledge Layer                | ❌ Not started           | items 42–45                              |
| — Phase 15: HITL Cloud Escalation              | ❌ Not started           | items 46–49                              |
| — Phase 16: Repair Episode Recording           | ❌ Not started           | items 50–52                              |
| — Phase 17: Federated Case Library             | ❌ Not started           | items 53–55                              |
| — Phase 18: Code Proposals *(stretch)*         | ❌ Not started           | items 56–61                              |

---

## Remaining Work

**Execution order:** 4.5 → 5 → 6 → 2 → 7 → 8 → 9 → 10*(stretch)*. The milestone numbers reflect original sequencing; the phases deliver them in this order. See `docs/implementation-plan.md` Phase 11–18 for item-level detail.

---

### Milestone 4.5 — HA Resource Stewardship

**Objective:** Keep the HA Yellow's disk from filling up, which would cause `ha backups new` to fail and break the entire pipeline. Monitor disk and memory continuously; offload backup files to Pueo's machine after each backup; enforce a retention policy that keeps HA clean.

**Why here first:** This protects the safety invariant. Every other milestone depends on backups succeeding. It builds only on existing SSH + SQLite infrastructure — no new architecture needed.

**Tasks:**
- Poll `ha host info` via SSH on a configurable interval; extract disk and memory; surface HITL alerts when thresholds are crossed; block backup trigger when disk < CRITICAL
- Extend `backup_registry` with `size_bytes`, `location` (`ha`/`pueo`/`both`), `offloaded_at`, `deleted_from_ha_at`
- After each confirmed backup slug: SFTP-pull the `.tar` to `BACKUP_LOCAL_DIR`; SHA-256 verify; update inventory
- Retention policy: keep `BACKUP_RETAIN_ON_HA` (default 2) most-recent on HA; purge local copies older than `BACKUP_RETAIN_LOCAL_DAYS` (default 30 days); never delete from HA without confirmed local copy
- `--mode backup-status` inventory table; dashboard backup tab

**Validation gate:** HA backup count stays ≤ 2; every backup has a local Pueo copy; HITL alert fires when disk drops below warning threshold in a test scenario.

Full spec: [plan/resource-stewardship.md](plan/resource-stewardship.md)

---

### Milestone 2 — Local RAG & Knowledge Layer

**Objective:** Keep the agent knowledgeable about HA breaking changes and integration updates without live web searches, satisfying the 0 WAN packets constraint.

**Delivered in Phase 14 (after the tool loop).** Originally planned as `[KNOWLEDGE]` block injection into a fixed prompt. Redesigned as a `query_knowledge` tool registered in the Phase 13 tool loop — the agent queries for context only when it judges it useful, avoiding token waste on irrelevant chunks.

**Tasks:**
- Stand up ChromaDB locally on macOS; embed with `nomic-embed-text` via Ollama (zero WAN)
- Weekly scrapers for: HA core release notes (breaking changes section), HACS component changelogs
- `query_knowledge` tool registered in the tool registry; returns top-K ranked chunks with source metadata
- `community_cases` ChromaDB collection created here (empty until Milestone 9 / Phase 17 delivers cases)
- Weekly refresh via macOS `launchd` plist

**Validation gate:** Agent correctly cites a specific HA breaking change from the local vector DB, zero WAN calls.

Full spec: [plan/rag-tool.md](plan/rag-tool.md)

---

### Milestone 5 — Agent Quality & Evaluation

**Objective:** Make regressions visible. Without evals, there is no way to know if a prompt change, model upgrade, or new feature makes the agent better or worse at its actual job. Unit tests verify code correctness; evals verify agent intelligence.

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

### Milestone 7 — HITL Cloud Escalation

**Objective:** When the local tool loop exhausts its budget without a fix, offer to escalate to Claude (Anthropic API). User approves per-incident. The cloud model runs with the same tool registry and sees the full step history from the failed local loop.

**Tasks:**
- `ClaudeAPIClient` implementing `LLMClientProtocol`; tool adapter mapping Pueo's registry to Anthropic tool-use JSON schema; prompt caching on system prompt
- Escalation HITL card: cost estimate, failed tool-call summary, approve/reject
- Cloud response dispatched via the same Pueo tool execution layer (no new execution path)
- Billing guard: `CLOUD_MAX_COST_PER_INCIDENT_USD` (default $0.50), `CLOUD_MAX_DAILY_SPEND_USD` (default $5.00), `cloud_spend` SQLite table
- `CLOUD_ESCALATION_ENABLED = false` by default; `ANTHROPIC_API_KEY` from environment only

**Validation gate:** Escalation fires only when user approves; billing caps block over-budget requests; `ANTHROPIC_API_KEY` cannot be set in `config.yaml`.

Full spec: [plan/cloud-escalation.md](plan/cloud-escalation.md)

---

### Milestone 8 — Repair Episode Recording

**Objective:** After every successful repair cycle, serialize a structured `RepairEpisode` to SQLite: symptoms, tool sequence, hypothesis chain, fix applied, outcome, model used. Exportable as anonymized YAML to feed the Federated Case Library.

**Tasks:**
- `repair_episodes` SQLite table (new migration), `RepairEpisode` dataclass, serialization hook at `finish_repair`
- `--mode export-episodes --since <date>` → anonymized YAML (IPs, hostnames, device names replaced with placeholders)
- Episodes tab in HITL dashboard: list, filter, detail view, "Prepare for submission" button

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

**Objective:** When Pueo identifies a capability gap during a repair loop, it proposes a Python diff, validates it against CI in a sandboxed temp directory, and surfaces a HITL approval card to open a PR. Approved changes become reusable tools for every future incident.

**Tasks:**
- New tools: `read_source`, `propose_patch`, `sandbox_code` (subprocess, no network, 60s timeout), `open_pr`
- Code proposal HITL card: diff viewer, test output, approve/reject
- Safety-critical file block list: diffs touching `utils/autonomy.py`, `interfaces.py`, `config.py`, or the backup invariant chain require additional confirmation
- Security review: sandbox escape vectors; ADR 007

**Validation gate:** Agent proposes a new tool for a synthetic gap scenario; sandbox CI runs; HITL approval opens a real PR; safety-critical block list tested.

Full spec: [plan/code-proposals.md](plan/code-proposals.md)

---

## Evaluation Matrix

These constraints govern all ongoing development. Evaluate every new feature against them before merging.

| Constraint | Target | Mitigation if failing |
|---|---|---|
| Inference latency | < 4 seconds per agent step | Quantize model to `q4_K_M`; offload embedding layers to Apple Silicon AMX |
| Config hallucination | Zero on inputs up to 8,000 tokens | Sliding window log ingestion; pass only relevant config sections, not full directories |
| Un-backed writes | 0% — no production write without a confirmed backup slug | `execute_remote_backup()` raises on failure; pipeline aborts |
| WAN packets during fix cycles | 0 — all inference local | All LLM calls route to local Ollama; no external API calls permitted in agent code |
| HA disk free | ≥ `HA_DISK_CRITICAL_GB` at all times | Block backup trigger + offload older backups automatically before new backup fires |
| Backup location | 100% of slugs confirmed on Pueo before deleting from HA | SHA-256 gate; `location = 'both'` required before any HA-side delete |
| Tool loop budget | ≤ 20 tool calls per incident | Hard cap in `AgentLoop`; exhaustion triggers escalation offer, not silent failure |
| Loop wall time | ≤ 120 seconds | `asyncio` timeout wrapping `AgentLoop.run()`; same outcome as budget exhaustion |
| Local fix rate | ≥ 80% resolved without cloud escalation | Tune tool count + model size if falling below; cloud escalation is the fallback |
| Episode coverage | 100% of successful repairs recorded | `finish_repair` tool fires serialization unconditionally |
| WAN during cloud escalation | User-approved only | `CLOUD_ESCALATION_ENABLED = false` default; each escalation requires explicit HITL approval |

---

## Architectural Note

The original plan specified LangGraph or CrewAI as the agentic framework. Plain `asyncio` was chosen instead — the current state machine is simple enough that a full framework would add dependency weight without benefit. Revisit if the system grows to require multi-agent coordination or complex branching state graphs.
