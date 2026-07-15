# Pueo — Development Roadmap

## Milestone Status

| Milestone | Status | Module |
|---|---|---|
| 1. Read-only ingestion & diagnostics | ✅ Complete | `ha_agent_core.py` |
| 2. Local RAG & knowledge ingestion | ❌ Not started | — |
| 3. Safe execution / shadow mode | ✅ Complete | `ha_agent_sandbox_engine.py` |
| 4. Closed-loop autonomous healing | ✅ Complete (HITL gate pending) | `ha_agent_sandbox_engine.py` |

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

### Milestone 4 (remaining) — Human-in-the-Loop Gate

**Objective:** Prevent fully autonomous action on changes that are high-risk or irreversible at the HA architecture level.

**Scope:** HACS updates, database schema migrations, breaking integration replacements. Minor config syntax fixes do not require HITL.

**Implementation note:** The gate belongs *before* `execute_remote_backup()` — not between backup and write. Triggering a backup before pausing for human approval wastes a backup slot and misleads the backup registry. The decision of what constitutes "critical" needs to be defined (likely a second Ollama classification step with a `requires_hitl` boolean in the schema).

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
