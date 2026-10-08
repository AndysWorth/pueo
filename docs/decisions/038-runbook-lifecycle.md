# ADR 038 — Runbook lifecycle: signature-keyed, distilled, evidence-validated

**Status:** Accepted
**Date:** 2026-10-08

## Context

ADR 018 introduced `save_runbook` as the learning mechanism: after a successful repair
the LLM calls the tool, writes a prose runbook to ChromaDB and SQLite, and future
sessions retrieve it in Phase 1. An audit of the live system on 2026-10-08 found the
loop never closed:

- 0 candidate runbooks have ever been created across all real sessions
- `save_runbook` was called 0 times in 18 observed debug episodes
- 6 of 7 gap rows are test leakage (not real production gaps)
- The 1 real gap was caused by retrieval bugs, not a missing runbook
- `agent_memory` (`remember` calls) is also empty

Three structural causes explain why the LLM never calls `save_runbook`:

1. **The model must remember.** The tool is listed in the registry and mentioned in the
   prompt, but under budget pressure the model deprioritises housekeeping calls.
2. **Gaps misfire.** The automatic gap path in `AgentLoop` keys gaps on the session type
   (`"update_poll"`) rather than the symptom, fires on success when `query_knowledge`
   was not called, and does not capture `gap_description` or `summary_if_giving_up`.
3. **Gaps pollute retrieval.** Gap rows live in the `strategies` ChromaDB collection and
   are returned as `[KNOWN GAP]` context, biasing future sessions toward dead ends.

The per-session write approach also cannot support structured dedup, versioned merges,
or evidence-based trust — the features needed to make the knowledge base grow reliably.

## Decision

### 1. Learning becomes infrastructure (remove `save_runbook`)

`save_runbook` is removed from all tool registries. The LLM no longer writes runbooks
directly. Instead, `AgentLoop.run()` enqueues a low-priority `WorkItem` at session end
when the session qualifies for distillation (see §4). A separate `runbook_distiller.py`
makes a single structured LLM call and merges the result into the KB.

**Why:** Removing the tool from the registry means no change to the model's budget
allocation, prompt structure, or loop logic. The learning loop becomes an infrastructure
concern like RAG refresh — it happens automatically and reliably without depending on
the model remembering to call a tool.

### 2. Symptom signatures

Every agent session acquires a **signature** — a deterministic string derived from the
session's structured initial context, never from LLM text:

| Session source | Signature pattern |
|---|---|
| Repair issue | `repair:<domain>:<translation_key>` |
| HA / HAOS update | `update:<core\|os\|addon\|hacs>:<slug>` |
| Live log error | `log:<logger>:<exception_class>` |
| Lovelace entity issue | `lovelace:<entity_domain>:<failure_kind>` |
| Persistent notification | `notification:<id_prefix>` |
| Chat session | `chat:<top_retrieved_signature>`, else `chat:unclassified` |

The signature is the single key for dedup, gap merging, usage stats, and resolution.
Pure functions in `utils/knowledge/runbook_signature.py` compute it for each session
type; all code paths that need a key call these functions rather than constructing
strings inline.

### 3. Structured runbook schema

All runbooks — seed, candidate, and validated — share a YAML frontmatter schema.
Free-form prose runbooks from ADR 018 are superseded for newly created runbooks.
Existing seed prompt files continue to be seeded as before; new distilled runbooks use
the structured format.

Frontmatter fields:

```yaml
signature: repair:recorder:backup_not_available
domain: recorder
integrations: []          # optional HA integration slugs
ha_version_min: ""        # optional, same format as release notes
ha_version_max: ""
recommended_tools:        # ordered list of tool names for Phase 3
  - read_logs
  - run_ha_command
state: candidate          # seed | candidate | validated
version: 1                # bumped on merge
episode_refs: []          # list of episode_ids that informed this runbook
```

Body sections (natural language, LLM-generated):

1. **When to use** — trigger conditions and symptoms
2. **Hypotheses → evidence** — what to look for and which tools confirm it
3. **Root-cause tests** — explicit pass/fail checks before acting
4. **Fix** — ordered steps
5. **Verification** — how to confirm the fix worked
6. **Pitfalls** — known failure modes for this approach

### 4. Post-session distillation

A session **qualifies** for distillation when all of the following hold:

- The session finished with `outcome = "success"`
- At least 3 evidence-gathering tool calls were made (not counting the terminal tool)
- Either no runbook with the same signature exists in `agent_strategies`, or the
  session's tool sequence diverged from the retrieved runbook's `recommended_tools`

When a session qualifies, `AgentLoop.run()` calls
`get_work_queue_or_none().enqueue(WorkItem(..., priority=PRIORITY_LOW, dedup_key=f"distill_{signature}"))`.
The dedup key prevents concurrent distillations for the same signature from queuing up.
The `WorkItem` runs `runbook_distiller.py::distill_session()`.

Distillation is a **single structured LLM call** with `temperature=0` and a Pydantic
output schema (`DistilledRunbook`). Input: the session's `tool_calls` from the debug
episode. Output: a populated runbook in the schema above.

**Non-success distillation (gap path):** when `outcome != "success"` and the session
made at least one `query_knowledge` call that returned no relevant results, or when
`outcome = "failed"`, `distill_session()` writes a **gap** keyed by signature
(see §5) instead of a candidate.

### 5. Knowledge gaps — separate from retrieval

Gaps move out of the `strategies` ChromaDB collection into:

- A `knowledge_gaps` ChromaDB collection, which is **never queried** by `query_knowledge`
  and is not in `COLLECTIONS`. It exists only for analytics and dashboard display.
- A `knowledge_gaps` SQLite table (V35 migration).

A gap row carries the signature, the queries tried, `gap_description`,
`summary_if_giving_up`, and the episode id. Gap keys are the signature, so multiple
failed sessions with the same symptom merge into one gap rather than creating duplicates.

**Auto-resolution:** when a qualifying distillation succeeds for a signature that has
an open gap, the distiller calls `resolve_gap(signature, episode_id)` — setting
`resolved_at` and linking the gap to the new candidate runbook.

The automatic `_maybe_auto_save_gap_runbook` in `AgentLoop` is removed. Gaps are
written only by the distiller, keyed properly on signature.

### 6. Runbook usage tracking

A new `runbook_usage` SQLite table records every injection and retrieval event:

```sql
CREATE TABLE runbook_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER REFERENCES agent_strategies(id),
    episode_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    event_type TEXT NOT NULL,  -- 'pre_injected' | 'retrieved'
    outcome TEXT,              -- filled at session end: 'success' | 'failed' | 'stuck'
    ts REAL NOT NULL
);
```

`AgentLoop` records the `strategy_id` values from pre-injection and from
`query_knowledge` calls, then writes `runbook_usage` rows at session end (via
`asyncio.to_thread` to avoid blocking).

Usage data drives two features:

- **Success-rate ranking boost** in `ChromaKnowledgeStore.query`: a runbook with ≥ 3
  successful episodes and ≥ 80% success rate gets a 1.15× blended-score multiplier.
- **Auto-validation:** a candidate becomes `validated` after
  `RUNBOOK_VALIDATE_MIN_SUCCESSES` (default 3, configurable) successes on distinct
  episodes with 0 failures. Only humans promote `validated` → `seed`.

### 7. Updated lifecycle states and authority scores

Full lifecycle:

```
seed (human-curated, prompts/ + pueo-kb)
  ↑ human promote only
validated (auto-promoted from candidate after N successes)
  ↑ auto after N successes + 0 failures
candidate (distilled from successful session)
  ↓ human discard → deleted (SQLite + Chroma)

knowledge_gaps (SQLite + separate collection, never retrieved)
  → resolved on later success with same signature
```

Updated authority scores replacing the ADR 030 tier table for runbooks:

| Source | Authority score |
|---|---|
| `ha_docs` (HA component source) | 1.0 |
| `ha_concepts` (HA official docs) | 0.95 |
| `ha_release_notes` (HA changelog) | 0.90 |
| `ha_best_practices` | 0.90 |
| `strategies` seed runbook | 0.85 |
| `repair_history` (successful past repairs) | 0.70 |
| `strategies` validated runbook | 0.75 |
| `strategies` pueo-kb community runbook | 0.70 |
| `strategies` candidate runbook (unreviewed) | 0.50 |
| `knowledge_gaps` | not retrieved |

The `validated` tier (0.75) sits above `repair_history` (0.70) to reflect that a
validated runbook is a structured, confirmed recipe rather than a raw episode record.
Candidate authority rises from 0.30 (ADR 030) to 0.50 to reflect that distilled
candidates are structured and outcome-linked, unlike the freeform LLM-written proposals
ADR 030 was scoring.

Updated authority labels in `query_knowledge` output:

| State | Label |
|---|---|
| seed | `[SEED RUNBOOK]` |
| validated | `[VALIDATED RUNBOOK]` |
| pueo-kb community | `[COMMUNITY RUNBOOK]` |
| candidate | `[CANDIDATE RUNBOOK – unreviewed]` |

### 8. Schema migration V35

Both `ha_agent_advanced.py` and `ha_agent_sandbox_engine.py` (per ADR 001 dual-file
rule):

- `agent_strategies` gains columns: `signature`, `version`, `episode_refs` (JSON),
  `resolved_at`, `resolved_by`
- `agent_strategies.runbook_state` gains allowed values `validated`, `resolved`,
  `discarded`
- New table `runbook_usage` (see §6)
- New table `knowledge_gaps` (signature, title, queries_tried, gap_description,
  summary, episode_id, resolved_at, resolved_by_episode_id)

Rollback: revert the commit. V35 only adds columns and tables; the prior code ignores
new columns and the new tables are empty if not written.

## Rationale

**Infrastructure over prompt.** Prompting the LLM to save runbooks produced zero
results across all real sessions. Making learning a post-session infrastructure job —
like embedding repair episodes or syncing Chroma — removes the dependency on the LLM
remembering a housekeeping step.

**Signatures over free keys.** Session-type keys (`"update_poll"`) collapse unrelated
problems. Symptom signatures (`update:core:homeassistant`) are specific enough for
meaningful dedup but stable enough across sessions to accumulate usage stats.

**Structured schema.** A defined schema with frontmatter enables filtering, ranking,
and merge logic that is impossible with freeform prose. The schema is minimal — only
what the distiller and retrieval ranking actually use.

**Gaps as analytics, not retrieval.** Returning `[KNOWN GAP]` chunks to the model
biases future sessions toward dead ends that prior sessions couldn't resolve. Moving
gaps out of retrieval makes the knowledge base signal noise — every retrieved runbook
represents something that worked.

**Evidence-based trust over human-only gate.** Manual promotion was the only path to
trust in ADR 018 and ADR 030. Adding an auto-validation path means a genuinely useful
runbook becomes trusted at 0.75 authority without requiring a code owner review of
every new candidate.

## Consequences

- `save_runbook` is removed from all registries and its prompt obligations (in
  `prompts/agent_loop.md`, `REQUEST_ESCALATION`, and all `seed_*.md` files) are dropped.
- `_maybe_auto_save_gap_runbook` in `AgentLoop` is removed.
- `runbook_distiller.py` is a new module; its `DistilledRunbook` schema gets three
  tests (valid, invalid, round-trip), per project convention.
- `runbook_signature.py` is a new module; every session type's pure function gets a
  test.
- All `AgentLoop` construction sites gain a `signature` parameter; all 7 agent
  pipelines must pass a computed signature at loop construction time.
- `RUNBOOK_VALIDATE_MIN_SUCCESSES` is a new config key (triple-update rule).
- The `COLLECTIONS` constant in `knowledge_store.py` does **not** include
  `knowledge_gaps` — it must not appear in any `query_knowledge` path.
- Dashboard `/runbooks` shows the new `validated` state, `knowledge_gaps` grouped by
  signature, usage counts, and success rates. Contribution sends `validated` runbooks
  to pueo-kb (S7).
- ADR 030 authority tier table is superseded by §7 of this ADR for runbook entries.
  Non-runbook tiers (ha_docs, ha_concepts, ha_release_notes, ha_best_practices,
  repair_history, community_cases) are unchanged.

## Related decisions

- [ADR 018 — Unified Agent Methodology](018-unified-agent-methodology.md): superseded
  for the runbook/learning sections (§§ `save_runbook tool`, `Strategy seeding`,
  `Three runbook states`, `Runbook gap mandate`, `LLM as KB contributor`). The 6-phase
  investigation cycle and all other sections of ADR 018 remain in effect.
- [ADR 030 — Authority-ranked knowledge](030-authority-ranked-knowledge.md): the
  runbook authority tier rows in §1 are superseded by §7 of this ADR. §§2–4 of ADR 030
  (blended sort, labels, version metadata) are unaffected.
- [ADR 025 — Serialized work queue](025-serialized-work-queue.md): distillation
  WorkItems use `PRIORITY_LOW` and must not bypass the no-concurrent-LLM rule.
- [ADR 026 — No concurrent LLM/HA](026-no-concurrent-llm-ha.md): the distillation
  WorkItem is a judgment-call LLM use and routes through `PueoWorkQueue`.
- [ADR 001 — Config centralization](001-config-centralization.md): `RUNBOOK_VALIDATE_MIN_SUCCESSES`
  triple-update rule (config.py + config.yaml.default + setup.sh).
- [ADR 031 — Repair episode embedding](031-repair-episode-embedding.md): distillation
  uses `episode_data.json` tool-call traces as primary input, not the episode prose
  summary.
