# ADR 018 — Unified Agent Methodology: 6-Phase Cycle and Strategy Learning

## Status
Accepted

## Context
Pueo's LLM-guided reasoning evolved into two patterns that should be unified:

**Static routing** — `prompts/agent_loop_chat.md` contained a hardcoded list of question-type playbooks (disk questions, security notifications, integration errors). Each new scenario required a new playbook section. There was no mechanism for Pueo to learn from successful sessions and apply that knowledge to future ones.

**Bypass** — Several diagnosis and repair pipelines (NetAlertX installer, config analysis, update personalization) used one-shot LLM calls with hardcoded evidence-gathering steps, missing the benefit of tool-calling iteration.

The immediate trigger was a production incident: the chat agent was asked about a `log_stream_reset` error from Pueo's own logs. It had no tool to read Pueo's logs, no mandatory knowledge retrieval step, and fell back to a hallucinated answer about a NOAA Tides API outage. Three gaps combined to produce the failure:

1. No mandatory `query_knowledge` first step — the model skipped retrieval and guessed
2. No `read_pueo_log` tool — the model could not inspect what the error actually was
3. Hardcoded playbook routing — the model hit a pattern it didn't recognize and fell through

ADR 012 (Hypothesis-driven repair cycle) introduced the right five-phase investigation pattern for HA repair loops but did not generalize it to chat sessions or other agent modes.

## Decision
Adopt the **6-Phase Investigation Cycle** as the universal pattern for all Pueo agent sessions:

```
Phase 1 — RETRIEVE CONTEXT: call query_knowledge first
Phase 2 — FORM A HYPOTHESIS: state it before calling any tool
Phase 3 — GATHER EVIDENCE: targeted reads, commands, doc lookups
Phase 4 — CONFIRM ROOT CAUSE: state it explicitly before acting
Phase 5 — ACT: apply fix / recommend / save_strategy
Phase 6 — REPORT: call terminal tool
```

This is encoded in `prompts/agent_loop_base.md` (the shared base prompt) and incorporated into `prompts/agent_loop_ha.md` and `prompts/agent_loop_chat.md`.

### `save_strategy` tool

New tool registered in the HA repair, NetAlertX, and chat registries. When the agent uses a novel approach that resolved an issue, it calls `save_strategy(title, trigger_pattern, approach)`:
- Embeds the approach text into the `strategies` ChromaDB collection (new, added to `COLLECTIONS`)
- Inserts a row in the `agent_strategies` SQLite table (migration v24)

The `strategies` collection is queried alongside `ha_release_notes`, `hacs_changelogs`, `ha_integration_docs`, and `community_cases` in all `query_knowledge` calls. Future sessions that encounter a similar trigger pattern will retrieve the learned approach in Phase 1.

### Strategy seeding at RAG refresh

At each RAG refresh, `utils/knowledge/strategy_seeder.py::seed_strategies()` embeds Pueo's existing playbook prompt files (`diagnose_netalertx.md`, `diagnose_installer.md`, `triage_repair_issue.md`, `investigation.md`, etc.) into the `strategies` collection. These seed documents cover common scenarios and are idempotent (upsert by filename-based ID).

### Log reading tools

Two new tools address the missing Pueo self-log capability:
- `read_pueo_log(lines, level)` — reads from `paths.get_dirs().log_dir / "pueo.log"` with optional level filter
- `search_log(log_name, pattern, context_lines, max_matches)` — regex search over `pueo` or `ha_core` logs

Both are registered in all agent registries.

## Runbook-Driven Planning

### Shift in framing: query_knowledge as plan retrieval

Phase 1 (`query_knowledge`) is not just a context lookup — it is a **plan retrieval call**. The `strategies` ChromaDB collection is Pueo's expert memory. When a runbook exists for the current situation, the agent follows it. When none exists, the agent reasons from first principles but always records the outcome for future sessions. Over time the knowledge base grows richer with each novel investigation, making Pueo an expert on the specific HA installation it manages.

### Three runbook states

| State | Created by | Definition |
|---|---|---|
| `seed` | Human-curated at development time | Authoritative starting point for a known query class. Pre-embedded at RAG refresh via `strategy_seeder.py`. |
| `candidate` | LLM via `save_runbook(type="candidate")` during a successful session | Novel approach that worked; not yet community-validated. Surfaces in dashboard for human review. |
| `gap` | LLM via `save_runbook(type="gap")` on any failure or empty KB result | Investigation record: what was tried, what was ruled out, why stuck. Tells future sessions what NOT to try and surfaces coverage holes to developers. |

Runbooks are rich prose (natural language). A YAML frontmatter header (`trigger_pattern`, `recommended_tools`, `state`) enables filtering and dashboard display. LLMs interpret and adapt the prose — no rigid schema required.

### Multi-phase query_knowledge

The agent may call `query_knowledge` at any phase, not only Phase 1. Additional calls are appropriate when:
- Initial evidence narrows the problem to a specific sub-domain — query with that sub-domain as the question
- Confidence is low after Phase 3 — try a differently-framed query
- About to try a novel approach — check whether a similar approach has been tried before
- Stuck — "pop up" to a higher-level framing and re-query broadly

### Confidence-based stopping (not count-based)

The agent does not give up because it has hit an arbitrary tool count. It stops only when it has genuinely exhausted all reasonable investigative paths and can document why. When stuck, the agent must:
1. Call `save_runbook(type="gap")` documenting what was tried, what was ruled out, and the best current understanding even if confidence is low
2. Call `request_escalation(reason)` — infrastructure routes per `ESCALATION_PREFERENCE`

### Runbook gap mandate

Any session that (a) had `query_knowledge` return nothing relevant, OR (b) ended with `outcome=failed`, ALWAYS saves a gap runbook. This applies to all agent modes (chat, repair, NetAlertX).

### LLM as KB contributor (with human gate)

When the agent uses a novel approach that worked, it calls `save_runbook(type="candidate")`. The candidate enters the KB tagged as "proposed by LLM, not yet community-validated." Humans can promote to `validated` or discard via the dashboard runbook review UI. Repeated success by multiple sessions auto-promotes the runbook.

### Escalation mechanism

New config key `ESCALATION_PREFERENCE: "hitl" | "cloud" | "cloud_then_hitl"` (default `"hitl"`). When stuck, the agent calls `request_escalation(reason)` and infrastructure routes per config — surfacing a HITL card, starting a cloud escalation loop, or doing both in sequence.

## What to leave as one-shot

These remain one-shot calls and should not be converted to AgentLoop sessions:

| Function | Reason |
|---|---|
| `ha_log_monitor.analyze_log_line_with_ai()` | Hot streaming path — loop latency harmful |
| `netalertx/log_monitor.analyze_log_line_with_ai()` | Same reason |
| `ha_update_manager.analyze_breaking_changes()` | Pure text analysis — no benefit from iteration |
| `ha_update_manager._self_check_llm_cross_reference()` | Pure text comparison |
| `ha_notification_manager` enrichment pipeline | Data gathering, not reasoning |

## Rationale

Phase 1 (`query_knowledge` first) is the most impactful single change. The model is most likely to retrieve relevant context before it has formed any opinion. After a hypothesis is formed, the model anchors to it and is less likely to be swayed by retrieved alternatives.

`save_strategy` closes the learning loop: the model is not just consuming the knowledge base, it is contributing to it. This is the mechanism that allows Pueo to improve its own reasoning without code changes — every novel approach that worked becomes available to future sessions via Phase 1 retrieval.

The log reading tools fix the most obvious gap directly rather than requiring workarounds.

## Consequences

- `prompts/agent_loop_base.md` is the canonical statement of Pueo's investigation pattern; all prompt files that implement agent sessions must follow this structure
- `COLLECTIONS` in `knowledge_store.py` now has 5 entries; `ChromaKnowledgeStore` creates all 5 at startup
- `agent_strategies` SQLite table is migration v24 in both migration files (see ADR 001 migration dual-file rule); the table gains a `runbook_state` column (Phase 2) to record seed/candidate/gap
- `save_runbook` (renamed from `save_strategy`) in all three non-code-proposal registries — any future registry must include it; `type` parameter (`seed`/`candidate`/`gap`) stored in `runbook_state` column
- Future `AgentLoop`-based sessions must pass a `knowledge_store` to the `ToolExecutor` so `save_runbook` can embed into ChromaDB; SQLite fallback fires when `knowledge_store` is None
- `seed_strategies()` is idempotent — re-seeding at RAG refresh is safe and keeps strategy content current as prompt files change
- Dashboard gains a runbook review UI (Phase 2): promote candidate → validated, discard, view gap runbooks
- `ESCALATION_PREFERENCE` config key (Phase 2): triple-update rule (`config.py` + `config.yaml.default` + `setup.sh`); values `hitl` (default), `cloud`, `cloud_then_hitl`
- Candidate runbooks are flagged in `query_knowledge` results so the agent knows which approaches are LLM-proposed vs. human-validated

## Related decisions
- [ADR 012 — Hypothesis-driven repair cycle](012-hypothesis-driven-repair.md): superseded and generalized by this ADR. The 5-phase repair cycle becomes the 6-phase universal cycle by adding Phase 1 (retrieve context).
- [ADR 013 — Prompt externalization](013-prompt-externalization.md): `prompts/agent_loop_base.md` follows the same convention; prompt content lives in `.md` files, not inline strings.
- [ADR 014 — Episodic context injection](014-episodic-context-injection.md): the `_retrieve_similar_episodes()` pre-loop infrastructure retrieval is superseded by Phase 1 (`query_knowledge`) — the agent now retrieves relevant context itself rather than having it injected by infrastructure code.
- [ADR 010 — Agent self-awareness](010-agent-self-awareness.md): `read_source` availability in all registries remains; the new log tools extend this pattern to Pueo's runtime output.
