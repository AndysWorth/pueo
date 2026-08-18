# LLM Architecture Upgrade Plan

**Status:** Draft — pending chunk selection  
**Extends:** Milestone 12 (agent self-knowledge + HA live lookup)  
**Date:** 2026-08-18

---

## Purpose

This document does three things:

1. Surveys the current state of the art for structuring LLM-powered agents and maps each pattern to Pueo's architecture.
2. Identifies structural gaps in Pueo's current LLM usage through a codebase audit.
3. Proposes a set of independent, session-sized implementation chunks in priority order.

The user will choose which chunks to implement. Each chunk can be delivered as a standalone PR.

---

## Part 1 — State-of-the-Art Agent Architecture Survey

The following patterns represent the mainstream approaches for structuring LLM reasoning in agent systems as of mid-2025. Each is described, evaluated for Pueo-fit, and rated for suitability.

---

### Pattern 1: ReAct (Reasoning + Acting)

**What it is:** The LLM interleaves explicit reasoning ("Thought: I need to check the config because...") with tool calls ("Action: read_config") and observes results ("Observation: found malformed YAML in automation block"). The key insight is that the reasoning trace is part of the conversation history, which improves subsequent decisions.

**Pueo status:** PARTIALLY IMPLEMENTED.  
`AgentLoop` is structurally a ReAct loop — the LLM calls tools, sees results, decides next. But the system prompt does not encourage explicit reasoning traces before each tool call. The model typically jumps straight to a tool call without verbalising its hypothesis, which means the reasoning is implicit and not reused in later steps.

**Gap:** The repair system prompt (`_AGENT_LOOP_SYSTEM_PROMPT` in `utils/agent_loop.py`) should instruct the model to state a one-sentence hypothesis *before* calling each tool. This improves chain-of-thought for small 7B models significantly.

**Fit for Pueo:** High. Costs nothing, improves 7B model reliability.

---

### Pattern 2: Plan-and-Execute (Two-Phase)

**What it is:** A planning LLM call generates a structured list of investigation steps; a separate execution phase runs each step. The plan can be inspected or modified before execution begins. This decouples "what to do" from "how to do it."

**Pueo status:** NOT IMPLEMENTED.  
Every agent loop goes straight from initial context to tool-calling. There is no explicit planning step. This sometimes causes the model to call `apply_fix` prematurely after reading only one file.

**Gap:** A brief planning call before the repair loop would let the model decide whether to: (a) read config only, (b) read logs + config, or (c) query the knowledge base first. For local 7B models this reduces wasted tool calls and premature fixes.

**Fit for Pueo:** Medium. Adds one LLM call per repair cycle. Worth implementing for repair loops, not for chat.

---

### Pattern 3: Self-Critique / Reflection

**What it is:** After generating a response or fix proposal, the model critiques it against a checklist ("Does this fix remove any critical keys? Does it address the root cause?"), then either approves or revises. Related to Constitutional AI's critique-revision cycle.

**Pueo status:** PARTIALLY IMPLEMENTED.  
`_review_limit()` in `AgentLoop` is a self-critique for the *process* (was budget well used?). But there is no self-critique for the *fix content* — the model proposes YAML and immediately calls `apply_fix` with no intermediate reflection step.

**Gap:** The repair system prompt should encourage the model to explicitly validate its proposed fix in reasoning before calling `apply_fix`. A structured "verify before acting" nudge in the prompt costs zero extra LLM calls.

**Fit for Pueo:** High for repair safety. Free (prompt-only change).

---

### Pattern 4: Hypothesis-Driven Diagnosis

**What it is:** Before gathering evidence, the model states one or more hypotheses about what might be wrong. Each evidence-gathering step is chosen to confirm or refute a hypothesis. When a hypothesis is confirmed, the model proposes a fix targeted at the root cause — not the symptom. This is the standard approach in automated root-cause analysis (RCA) systems.

**Pueo status:** NOT EXPLICITLY IMPLEMENTED.  
The current repair system prompt says: "read_config / read_logs → apply_fix → finish_repair." This is a symptom-driven flow. The model reacts to what it finds rather than testing a hypothesis.

**Gap:** The repair system prompt should be restructured around the diagnosis cycle: form hypothesis → gather evidence to test it → confirm root cause → propose targeted fix. This is the core of the `INVESTIGATION_SYSTEM_PROMPT_TEMPLATE` already written in `utils/investigation_loop.py` but never applied to the repair loop.

**Fit for Pueo:** High. Especially important for 7B models which benefit from explicit structure. The investigation_loop.py already has a working prompt template to adapt.

---

### Pattern 5: Episodic Memory Retrieval

**What it is:** Before starting a new task, the agent queries a memory store (vector DB or SQL) for similar past episodes. The retrieved episodes are injected into the initial context as examples. This is a form of few-shot prompting with personalized, verified examples rather than curated static examples.

**Pueo status:** INFRASTRUCTURE EXISTS, NOT WIRED TO REPAIR.  
`repair_episodes` and `community_cases` exist in SQLite and ChromaDB respectively. The `query_knowledge` tool lets the model query ChromaDB during a session. But the repair loop does *not* proactively inject relevant past episodes before the model starts reasoning.

**Gap:** At the start of each repair cycle, query the `community_cases` collection using the log line or initial context as the search query. Inject the top-1 or top-2 most similar past episodes as examples in the initial context. This gives the model a verified reference fix for known failure patterns.

**Fit for Pueo:** High. Uses existing infrastructure. Reduces hallucinated fixes on known failure modes.

---

### Pattern 6: Agentic RAG (Active Retrieval)

**What it is:** The model decides *when* to query the knowledge base, *what* to search for, and *whether the result is sufficient* to proceed. Contrast with passive RAG (always inject top-K chunks before the prompt). Agentic RAG is already the design in Pueo (query_knowledge is a tool), but requires the system prompt to actively encourage early consultation.

**Pueo status:** IMPLEMENTED but underutilised.  
`QUERY_KNOWLEDGE` is in the HA tool registry. But the repair system prompt does not mention it at all. Evals show the model rarely calls it during repair unless the config is explicitly flagged as an integration issue.

**Gap:** The repair system prompt should list `query_knowledge` in the recommended flow: "For integration-related issues, call query_knowledge first to check for known breaking changes before examining config." A single sentence in the prompt is all that is needed.

**Fit for Pueo:** High. Zero implementation cost.

---

### Pattern 7: Tool Self-Awareness (Meta-Tool Loop)

**What it is:** The agent can read its own tool registry and capability code. When encountering an unfamiliar situation, it inspects its available tools before deciding which to use — avoiding hallucinated tool names or incorrect parameter shapes. This is exactly what Milestone 12 (ADR 010) specifies with `read_source`.

**Pueo status:** NOT IMPLEMENTED for repair/NetAlertX registries.  
`read_source` is in chat and code-proposal registries only. The repair agent cannot inspect its own tool list. The NetAlertX healer cannot either.

**Gap:** This is Milestone 12, Chunk A. Two one-line additions to `build_ha_tool_registry()` and `build_netalertx_tool_registry()` in `utils/tool_registry.py`.

**Fit for Pueo:** High. Minimal implementation effort.

---

### Pattern 8: HA Live Lookup (Domain-Specific Tool)

**What it is:** For domain-specific agents, the most powerful tool is often the authoritative source for the domain. For Pueo, that is HA component source code on GitHub. When the agent is uncertain about a config key's valid values, it can fetch `const.py` for the relevant integration.

**Pueo status:** NOT IMPLEMENTED.  
This is Milestone 12, Chunk B: the `fetch_ha_docs` tool.

**Gap:** New `FETCH_HA_DOCS` ToolDefinition, `_fetch_ha_docs()` executor, WAN gate, cache, filename allowlist, `HA_SOURCE_CACHE_DIR` config key, RAG pre-fetch extension.

**Fit for Pueo:** High. Reduces hallucinated config key names.

---

### Pattern 9: Multi-Agent Separation of Concerns

**What it is:** Separate agents for distinct roles: a Triage Agent (read-only, forms hypothesis), an Executor Agent (proposes and applies fix), a Verifier Agent (confirms the fix worked). Each agent's scope is narrow and its system prompt is tight. Reduces scope creep and premature action.

**Pueo status:** PARTIALLY IMPLEMENTED.  
`investigation_loop.py` (`run_investigation`) is exactly a Triage Agent — read-only, ends with `finish_investigation`, never writes. But the output of `run_investigation` is never fed as input to the repair `AgentLoop`. The two loops are used in entirely separate code paths.

**Gap:** Wire `run_investigation()` output into the repair loop's `initial_context`. The investigation summary, root_causes, and hitl_actions become the starting context for the repair agent, which only needs to execute a fix, not re-derive the diagnosis.

**Fit for Pueo:** Medium-High. Requires a bit of plumbing. Best applied to the disk recovery and repair-issue paths first.

---

### Pattern 10: Structured Terminal Outputs

**What it is:** The terminal tool (the "I'm done" signal) requires structured, machine-parseable fields rather than free text. This lets the caller make decisions based on the agent's self-assessment rather than parsing prose.

**Pueo status:** WELL IMPLEMENTED.  
`finish_repair` already requires `action_taken` (enum), `capability_gap` (bool), `gap_description` (string). `finish_investigation` requires structured `root_causes`, `auto_actions`, `hitl_actions` with risk levels. This is a strength of the current design.

**Gap:** Minor. The `finish_chat` terminal tool only requires a free-text `summary`. For longer sessions, a structured `facts_learned: list[str]` field would support richer automatic memory writes.

**Fit for Pueo:** Low priority. Existing structure is already good.

---

### Patterns Not Suitable for Pueo

| Pattern | Why not applicable |
|---|---|
| Tree of Thoughts | Too many LLM calls; local 7B models too slow for branching |
| Constitutional AI multi-round critique | Too many round-trips; time-sensitive repair loop |
| Parallel agent execution | asyncio + single local GPU; true parallelism would stall |
| Long-context summarization (mid-loop) | Context window isn't the bottleneck yet; revisit at 32B models |

---

## Part 2 — Codebase Audit

### What is using LLM well (AgentLoop)

| Subsystem | File | Notes |
|---|---|---|
| HA repair | `ha_agent_sandbox_engine.py` | AgentLoop with all tools; capability gap → code proposal loop |
| NetAlertX healing | `netalertx/healer.py`, `netalertx/one_shot_diagnose.py` | AgentLoop via `heal_with_loop()` |
| Investigation | `utils/investigation_loop.py` | Read-only AgentLoop variant, good structure |
| Chat | `web/dashboard.py` | AgentLoop per user message; per-session history |

### What uses direct LLM calls (single-shot structured generation)

These are appropriate when latency is critical or the task is genuinely one-shot. Most should stay as-is; some could benefit from minor prompt improvements.

| File | Function | Schema | Notes |
|---|---|---|---|
| `ha_log_monitor.py` | `analyze_log_line_with_ai()` | `LogEvaluation` | Triage gate; high frequency; stays single-shot for latency |
| `ha_notification_manager.py` | `analyze_notification()` | `_NotificationLLMOutput` | Explanation/triage of persistent notifications |
| `ha_update_manager.py` | `analyze_breaking_changes()` | `UpdateReadinessReport` | Release note analysis; appropriate single-shot |
| `ha_update_manager.py` | `_self_check_llm_cross_reference()` | `SelfCheckCommandRisk` | Post-update command catalog check |
| `netalertx/diagnosis.py` | `diagnose_health_report()` | `NetAlertXDiagnostic` | Health report classification |
| `netalertx/log_monitor.py` | `analyze_log_line_with_ai()` | `LogEvaluation` | Same pattern as HA log triage |
| `netalertx/installer_diagnostics.py` | `diagnose_installer_failure()` | installer diagnostic | Step-failure diagnosis during install |

### Structural issues found

**Issue 1: Inline prompts** — `ha_notification_manager.py` and `ha_update_manager.py` define their system prompts as inline string literals instead of using `load_prompt()` from the `prompts/` directory. This makes them invisible to anyone browsing `prompts/` and harder to tune. Three call sites affected.

**Issue 2: Hardcoded model in log monitor** — `ha_log_monitor.py::analyze_log_line_with_ai()` passes `model=_config.OLLAMA_MODEL` directly instead of using `_default_model_for_provider()`. When `LLM_PROVIDER=cloud`, the log triage call still uses the Ollama model name. This bypasses the provider abstraction.

**Issue 3: `agent_loop_ha.md` is a dead file** — `prompts/agent_loop_ha.md` exists but is never loaded. The active system prompts are inline strings in `agent_loop.py`. The `prompts/` file should either be adopted or removed.

**Issue 4: Repair system prompt is skeletal** — `_AGENT_LOOP_SYSTEM_PROMPT` (7 lines) gives the model a basic "call tools, call finish_repair" mandate but no guidance on: investigation strategy, when to query the knowledge base, when to use `read_source`, hypothesis formation, or verification after fixing. The chat system prompt is better but also lacks recall-before-answering guidance.

**Issue 5: HA repair issues lack LLM explanation** — `poll_for_repairs()` in `ha_log_monitor.py` classifies repair severity and action type using pure keyword matching (`"reboot" in translation_key.lower()`). Repair approval cards contain HA's raw `translation_key` strings rather than a human explanation of what the issue is and why the recommended action makes sense. Notifications get LLM-enriched explanations (`analyze_notification()`); repairs do not.

**Issue 6: Disk recovery bypasses LLM reasoning** — `utils/disk_recovery.py` makes all decisions procedurally. The module docstring says it is "a hardcoded instantiation" of the investigation pattern, and `investigation_loop.py` was written to be its generalisation. But `disk_recovery.py` never calls `run_investigation()`. The LLM has no role in deciding *which* recovery actions to take or in what order — it just runs a fixed sequence of SSH commands.

**Issue 7: Milestone 12 not yet implemented** — `read_source` is absent from `build_ha_tool_registry()` and `build_netalertx_tool_registry()`. The `fetch_ha_docs` tool does not exist yet.

---

## Part 3 — Proposed Implementation Chunks

Each chunk is designed to be a single PR, completable in one session. They are listed in recommended priority order but are largely independent.

---

### Chunk A: Milestone 12 — Agent Self-Knowledge + HA Live Lookup
*ADRs 010 and 011; specified in roadmap*

**Files:** `utils/tool_registry.py`, `utils/tool_executor.py`, `utils/ha_docs_scraper.py`, `config.py`, `config.yaml.default`, `setup.sh`, `utils/agent_loop.py`, `tests/test_tool_registry.py`, `tests/test_tool_executor.py`, `docs/decisions/010-agent-self-awareness.md`, `docs/decisions/011-ha-live-lookup.md`

**Changes:**
1. Add `READ_SOURCE` to `build_ha_tool_registry()` and `build_netalertx_tool_registry()` — one line each
2. Add `FETCH_HA_DOCS` ToolDefinition to `tool_registry.py`
3. Add `_fetch_ha_docs(domain, filename)` method to `ToolExecutor`: cache-first read from `HA_SOURCE_CACHE_DIR/{domain}/{filename}`; WAN-gated (`LLM_PROVIDER` check); filename allowlist (`__init__.py`, `manifest.json`, `config_flow.py`, `const.py`, `strings.json`, `*.md`); path-traversal guard; raises `ToolError` on local-mode cache miss
4. Register `FETCH_HA_DOCS` in `build_ha_tool_registry()`, `build_chat_tool_registry()`, `build_netalertx_tool_registry()`
5. Extend `ha_docs_scraper.py` with `prefetch_installed_integration_sources(domains, cache_dir)` — fetches `__init__.py`, `manifest.json`, `const.py` for each domain; called at end of RAG refresh cycle
6. Add `HA_SOURCE_CACHE_DIR` config key (triple-update: `config.py`, `config.yaml.default`, `setup.sh`)
7. Update `_AGENT_LOOP_SYSTEM_PROMPT` with 2-line note: call `read_source("utils/tool_registry.py")` when uncertain about available tools; call `fetch_ha_docs(domain, filename)` for HA component details

**Tests:**
- 3 registry membership tests (HA and NetAlertX registries include `read_source`; HA registry includes `fetch_ha_docs`)
- Cache hit: returns cached content, no HTTP call
- Local mode miss: `ToolError` raised, no HTTP call
- Cloud mode live fetch: HTTP called, content cached, returned (mock HTTP)
- Disallowed filename: `ToolError`
- Path traversal attempt: `ToolError`

**Validation:** In a chat session, `read_source("utils/tool_registry.py")` returns the tool list. `fetch_ha_docs("zha", "manifest.json")` with cache populated returns manifest content. Local mode + empty cache raises `ToolError`. CI passes.

---

### Chunk B: System Prompt Upgrade — Hypothesis-Driven Repair
*Addresses Patterns 1, 2, 3, 4, 6 from the SOTA survey*

**Files:** `utils/agent_loop.py`, `prompts/agent_loop_ha.md` (adopt or replace), `prompts/agent_loop_chat.md` (new), `prompts/agent_loop_ha.md`

**Changes:**
1. Restructure `_AGENT_LOOP_SYSTEM_PROMPT` around the hypothesis-driven cycle:
   ```
   INVESTIGATION CYCLE:
   1. Form a hypothesis (state it in reasoning before calling any tool)
   2. Call query_knowledge to check for known breaking changes first
   3. Gather evidence (read_config / read_logs / read_file) to test the hypothesis
   4. If a fix is needed, state the root cause before calling apply_fix
   5. Call verify_fix after apply_fix to confirm it worked
   6. Call finish_repair with what you found and did
   ```
2. Move `_AGENT_LOOP_SYSTEM_PROMPT` to `prompts/agent_loop_ha.md` and load via `load_prompt()`, replacing the inline string in `agent_loop.py`
3. Move `_CHAT_SYSTEM_PROMPT` to `prompts/agent_loop_chat.md` and load via `load_prompt()`
4. Add to chat system prompt: "Call `recall` at the start of any session where the user asks about their setup, preferences, or past issues, before answering."
5. Move inline prompts from `ha_notification_manager.py` (line 264) and `ha_update_manager.py` (lines ~172, ~648) to `prompts/` directory; use `load_prompt()`

**Tests:** Existing agent loop tests exercise the system prompt path — no new tests required for the prompt move, but verify no test breaks. Add one test confirming the prompt files load correctly.

**Validation:** Run one eval scenario and confirm the step trace shows `query_knowledge` being called before `read_config` when the log line mentions a known integration. Inspect the step trace in the dashboard to see hypothesis reasoning.

---

### Chunk C: HA Repair Issues — LLM Explanation

*Addresses Issue 5 (HA repair issues lack LLM explanation)*

**Files:** `ha_log_monitor.py`, `prompts/triage_repair_issue.md` (new), `tests/test_core_agent.py`

**Changes:**
1. Define `RepairIssueAnalysis(BaseModel)` with fields: `human_explanation: str`, `recommended_action_rationale: str`, `requires_hitl: bool`
2. Add `analyze_repair_issue(issue: dict, llm_client)` — single-shot LLM call using `triage_repair_issue.md` prompt; takes the HA repair issue dict (translation_key, severity, description) and returns `RepairIssueAnalysis`
3. Call `analyze_repair_issue()` inside `poll_for_repairs()` before building the approval card, replacing the keyword-match classification
4. The approval card body now includes `human_explanation` and `recommended_action_rationale` instead of the raw translation_key string
5. Write `prompts/triage_repair_issue.md` prompt

**Tests:**
- `test_analyze_repair_issue_returns_schema` — fake LLM returns valid JSON, asserts schema fields
- `test_analyze_repair_issue_invalid_json` — fake LLM returns garbage, function returns safe default
- `test_poll_for_repairs_uses_llm_explanation` — mock `analyze_repair_issue`, assert card body includes explanation

**Validation:** With a live HA instance that has a repair issue pending, the approval card in the dashboard shows a human-readable explanation instead of a raw translation_key string.

---

### Chunk D: Fix Hardcoded Model in Log Monitor

*Addresses Issue 2 (hardcoded OLLAMA_MODEL bypasses provider abstraction)*

**Files:** `ha_log_monitor.py`, `netalertx/log_monitor.py`

**Changes:**
1. In `ha_log_monitor.py::analyze_log_line_with_ai()`: replace `model=_config.OLLAMA_MODEL` with `model=_default_model_for_provider()` (import from `utils.llm_factory`)
2. Same fix in `netalertx/log_monitor.py::analyze_log_line_with_ai()`
3. Check `netalertx/diagnosis.py`, `netalertx/installer_diagnostics.py`, and `ha_notification_manager.py` for the same pattern — fix any found

**Tests:** One test per affected file confirming the model argument comes from the factory, not a hardcoded constant. Mock `_default_model_for_provider` and assert it is called.

**Validation:** With `LLM_PROVIDER=cloud`, log triage uses `CLOUD_MODEL`; with `LLM_PROVIDER=local`, it uses `OLLAMA_MODEL`. CI passes.

---

### Chunk E: Episodic Context Injection

*Addresses Pattern 5 (episodic memory retrieval before repair)*

**Files:** `ha_agent_sandbox_engine.py`, `utils/knowledge_store.py`, `tests/test_core_agent.py`

**Changes:**
1. Add `_retrieve_similar_episodes(initial_context: str, knowledge_store: KnowledgeStoreClientProtocol, top_k: int = 2) -> str` to `ha_agent_sandbox_engine.py`: queries `community_cases` ChromaDB collection with `initial_context` as search text; returns a formatted string with up to `top_k` similar past episodes (problem description + fix applied) or empty string if none found or store unavailable
2. Call this function at the start of `run_repair_loop()` (or equivalent entry point): if the returned string is non-empty, prepend it to `initial_context` as a "Similar past repairs:" block
3. Cap injected text at 1,000 tokens via `truncate_to_budget()`

**Tests:**
- `test_episodic_injection_prepends_context` — mock knowledge store returns two episodes; assert `initial_context` passed to `AgentLoop.run()` contains them
- `test_episodic_injection_empty_collection` — mock returns empty; assert `initial_context` is unchanged
- `test_episodic_injection_unavailable_store` — knowledge store raises; assert graceful fallback, no crash

**Validation:** With a seeded `community_cases` collection containing a known ZHA issue, a repair triggered by a ZHA traceback log line starts with the similar episode in context. Visible in the pre-step trace on the dashboard.

---

### Chunk F: Disk Recovery LLM Integration

*Addresses Issue 6 (disk recovery bypasses LLM reasoning)*

**Files:** `utils/disk_recovery.py`, `web/dashboard.py`, `tests/test_disk_recovery.py`

**Context:** `utils/investigation_loop.py` already has `run_investigation()` and `investigate_with_fallback()`. `disk_recovery.py` docstring says it is "a hardcoded instantiation of this pattern." This chunk replaces the heuristic decision-making with an LLM investigation, keeping the heuristic path as a fallback.

**Changes:**
1. Add `llm_client` and `knowledge_store` parameters to `run_disk_recovery()` (or the main disk-recovery entry function in the file)
2. Call `investigate_with_fallback(topic="HA disk space critically low", ...)` at the start, before running any SSH commands
3. If the investigation succeeds (not fallback): use `report.auto_actions` and `report.hitl_actions` to drive the recovery sequence instead of the hardcoded command sequence
4. If investigation times out or fails: fall back to the current heuristic sequence (no regression)
5. The `action_key` field on `InvestigationAction` maps to existing recovery action functions in `disk_recovery.py` via a dispatch table

**Tests:**
- `test_disk_recovery_uses_investigation_when_available` — mock `investigate_with_fallback` returning a report; assert recovery actions match report's `auto_actions`
- `test_disk_recovery_falls_back_to_heuristic` — mock raises; assert heuristic sequence runs
- `test_disk_recovery_action_key_dispatch` — assert each valid action_key maps to a callable

**Validation:** Trigger a disk recovery manually (lower the disk threshold temporarily). Observe in the dashboard timeline that the recovery sequence matches the LLM investigation's recommended actions, not a fixed script.

---

## Priority Matrix

| Chunk | Effort | Impact | Dependency | Recommended order |
|---|---|---|---|---|
| A — Milestone 12 | Medium | High (self-awareness, live lookup) | None | **1** |
| B — System Prompt Upgrade | Low | High (better 7B reasoning for free) | None (but A improves it) | **2** |
| D — Fix Hardcoded Model | Tiny | Medium (provider correctness) | None | **3** (fold into A or B) |
| C — HA Repair LLM Explanation | Low | Medium (UX improvement) | None | **4** |
| E — Episodic Context Injection | Medium | High (fewer hallucinated fixes) | Requires ChromaDB seeded | **5** |
| F — Disk Recovery LLM | Medium | Medium (principled recovery) | investigation_loop.py (exists) | **6** |

---

## What This Plan Does NOT Change

- `AgentLoop` core structure — the iterative loop is solid; only prompts and tool registries change
- The safety invariant — backup-before-write remains unchanged and is not touched by any chunk
- The provider abstraction — `LLMClientProtocol` and `make_llm_client()` are not modified
- Single-shot call patterns in log triage and notification analysis — these are correctly scoped as single-shot; no migration to AgentLoop is proposed

---

## Files Not Touched

- `utils/autonomy.py` — the autonomy gate is correct; no change
- `utils/repair_episode.py` — episode recording is already comprehensive
- `utils/billing.py` — spending caps unchanged
- `utils/rate_limiter.py` — debounce/rate-limit logic unchanged
- All test infrastructure — no new fixtures or conftest changes needed for most chunks
