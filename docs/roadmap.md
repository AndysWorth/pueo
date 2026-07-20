# Pueo — Development Roadmap

## Milestone Status

| Milestone | Status | Module |
|---|---|---|
| 1. Read-only ingestion & diagnostics | ✅ Complete | `ha_agent_core.py` |
| 2. Local RAG & knowledge ingestion | ❌ Not started | — |
| 3. Safe execution / shadow mode | ✅ Complete | `ha_agent_sandbox_engine.py` |
| 4. Closed-loop autonomous healing | ✅ Complete | `ha_agent_sandbox_engine.py` |
| 5. Agent quality & evaluation | ❌ Not started | `evals/` |

---

## Remaining Work

### Milestone 2 — Local RAG & Knowledge Layer

**Objective:** Keep the agent knowledgeable about HA breaking changes and integration updates without live web searches, satisfying the 0 WAN packets constraint.

**Tasks:**
- Stand up a local vector database (ChromaDB or Qdrant) on macOS
- Write a weekly scraper/parser that vectorizes:
  - Home Assistant core release notes (breaking changes section)
  - HACS component changelogs for installed components
  - High-traffic community forum threads on common integration failures
- Inject relevant retrieved context into the Ollama diagnostic prompt as a `[KNOWLEDGE]` block ahead of the YAML content
- Respect the 8,000 token context limit — retrieved chunks must be ranked and truncated

**Validation gate:** Query the agent on a specific breaking change from a recent HA release. It must accurately cite the change and identify affected YAML keys purely from the local vector DB, with zero live web calls.

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

## Evaluation Matrix

These constraints govern all ongoing development. Evaluate every new feature against them before merging.

| Constraint | Target | Mitigation if failing |
|---|---|---|
| Inference latency | < 4 seconds per agent step | Quantize model to `q4_K_M`; offload embedding layers to Apple Silicon AMX |
| Config hallucination | Zero on inputs up to 8,000 tokens | Sliding window log ingestion; pass only relevant config sections, not full directories |
| Un-backed writes | 0% — no production write without a confirmed backup slug | `execute_remote_backup()` raises on failure; pipeline aborts |
| WAN packets during fix cycles | 0 — all inference local | All LLM calls route to local Ollama; no external API calls permitted in agent code |

---

## Architectural Note

The original plan specified LangGraph or CrewAI as the agentic framework. Plain `asyncio` was chosen instead — the current state machine is simple enough that a full framework would add dependency weight without benefit. Revisit if the system grows to require multi-agent coordination or complex branching state graphs.
