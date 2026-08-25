# ADR 014 — Episodic context injection before repair loops

## Status
Superseded by [ADR 018 — Unified Agent Methodology](018-unified-agent-methodology.md)

Infrastructure pre-injection replaced by Phase 1 (`query_knowledge`) as the universal context-retrieval step. See ADR 018.

## Context
Every Pueo repair loop starts cold. The model receives the triggering log line or issue description and must reason from scratch about what is wrong and how to fix it. Two sources of relevant historical context exist but were not used:

1. **`repair_episodes`** (SQLite) — records of every previous Pueo repair: symptoms, tool sequence, fix applied, outcome.
2. **`community_cases`** (ChromaDB) — anonymised episodes from the federated case library, each with a problem description and a verified fix.

The `query_knowledge` tool allows the model to query ChromaDB *during* a session, but this requires the model to decide to query and to form an appropriate search. In practice, evals showed the model rarely called `query_knowledge` in the first two tool calls, meaning it would investigate from scratch before consulting prior art — even when a nearly identical case existed.

Episodic memory retrieval is a standard SOTA pattern for agent systems: retrieve similar past episodes *before* the loop starts and inject them as examples. This is analogous to few-shot prompting but with personalised, verified examples rather than curated static ones.

## Decision
At the start of each repair cycle (before `AgentLoop.run()` is called), `_retrieve_similar_episodes(initial_context, knowledge_store, top_k=2)` queries the `community_cases` ChromaDB collection using the triggering context as the search query. If similar episodes are found, they are prepended to `initial_context` as a "Similar past repairs:" block, capped at 1,000 tokens via `truncate_to_budget()`.

The retrieval is best-effort:
- If the ChromaDB store is unavailable or the collection is empty, the function returns an empty string and `initial_context` is used unchanged.
- If `knowledge_store` is `None` (dependency-injected), the injection step is skipped.
- No exception from the retrieval step may propagate to the repair loop — the loop must proceed regardless.

The injected block has the format:
```
Similar past repairs (use as reference, verify before applying):
1. Problem: <description> → Fix: <fix_applied>
2. Problem: <description> → Fix: <fix_applied>
```

## Rationale
Injecting retrieved episodes into the *initial context* rather than relying on the model to call `query_knowledge` provides two advantages:

1. **Guaranteed consultation.** The model sees relevant prior art before it forms its first hypothesis, not after it has already committed to an investigation strategy.
2. **No budget cost.** The retrieval is a direct ChromaDB query, not an LLM call. The injected text uses the same token budget that would otherwise be blank.

The 1,000-token cap and the `truncate_to_budget()` enforcement ensure that a large episode does not crowd out the actual issue context. The "verify before applying" qualifier in the injected block prevents the model from blindly copying a past fix without checking that it applies to the current situation.

## Consequences
- `ha_agent_sandbox_engine.py` gains a `_retrieve_similar_episodes()` helper and a call to it at the start of `run_repair_loop()`
- `KnowledgeStoreClientProtocol` is the injected dependency; `FakeKnowledgeStore` is the test double
- The pre-step trace visible in the dashboard may show a "context enriched with N past episode(s)" log line to make the injection visible
- If the `community_cases` collection is empty (fresh install, or federated case library not yet seeded), behaviour is identical to before this change
- Evals should include a scenario where a seeded community case causes the model to reach a correct fix in fewer tool calls than the cold-start baseline

## Related decisions
- [ADR 012 — Hypothesis-driven repair cycle](012-hypothesis-driven-repair.md): episodic context is prepended to the initial user message, not the system prompt; the two changes are independent and compose correctly.
- [ADR 008 — External resolution detection](008-external-resolution-detection.md): if a past episode is injected and the issue has already self-resolved, the loop's external-resolution check fires normally; the injected context does not suppress it.
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): the `RepairEpisode` dataclass (from `utils/repair_episode.py`) is the schema used to format injected episodes; only `problem_description` and `fix_applied` fields are included in the injected text.
