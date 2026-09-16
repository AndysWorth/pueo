# ADR 031 — Repair episode embedding and ADR 014 pre-injection completion

## Status
Accepted

## Context
ADR 014 (episodic context injection) described a pattern where similar past repair episodes would be retrieved from a ChromaDB collection and injected into the agent loop's initial context before the first LLM call. The intent was that the model would see relevant prior art — "the last time recorder DB was growing, Pueo reduced retention to 30 days" — before forming its first hypothesis.

The infrastructure was designed: `AgentLoop._pre_inject_knowledge()` exists and queries the knowledge store at loop start. The `repair_episodes` SQLite table has an `embedded_at` column added in migration V26. `format_episode_for_embedding()` exists in `utils/repair/repair_episode.py`.

What was missing:
1. No `repair_history` ChromaDB collection. `COLLECTIONS` in `knowledge_store.py` contained five entries; `repair_history` was not among them. `ChromaKnowledgeStore` only creates collections it knows about.
2. No embedder. The `embedded_at` column existed but nothing ever set it — all 6 production episodes had `embedded_at IS NULL`.
3. No RAG refresh step. `run_rag_refresh` had no step that called any episode-embedding function.

As a result, `_pre_inject_knowledge()` queried five populated collections and never surfaced a past repair episode — even when an identical issue had been resolved minutes earlier.

## Decision

### 1. Add `repair_history` to `COLLECTIONS`
`COLLECTIONS` in `utils/knowledge/knowledge_store.py` is the single authoritative list of ChromaDB collections. Adding `"repair_history"` causes `ChromaKnowledgeStore` to create and maintain the collection automatically and `query()` to include it in every default-collections call — no other changes to the query path are needed.

### 2. New `utils/knowledge/repair_episode_embedder.py`
`embed_repair_episodes(store, db_path) -> int` reads all rows where `embedded_at IS NULL` from `repair_episodes`, calls `format_episode_for_embedding()` on each, upserts the resulting chunks into `repair_history` with metadata (`outcome`, `model_used`, `trigger`, `timestamp`), then marks `embedded_at`. Returns the count embedded.

The function is best-effort: any failure at any step is logged and the caller receives 0 — the rag-refresh continues regardless. This matches the existing pattern of all other RAG refresh steps.

### 3. Wire into `run_rag_refresh` as step 7
`main.py::run_rag_refresh` gains step 7 (after step 6, re-embed orphaned runbooks): import and call `embed_repair_episodes(store, config.DB_PATH)`.

### 4. Log when past repairs are injected
`AgentLoop._pre_inject_knowledge()` logs `pre_inject_similar_episodes` at INFO level when any returned chunks come from `repair_history`. This makes the injection visible in the timeline without adding overhead for sessions where no past repairs are relevant.

## Rationale

The `_pre_inject_knowledge()` path is already correct — it queries the knowledge store once at loop start, no code change needed there. The only missing pieces were the collection definition and the population step.

Embedding at RAG refresh (not at `finish_repair` time) avoids embedding overhead on the critical repair path and keeps the Ollama embedding calls batch-able. The tradeoff is that episodes from a repair that just finished are not queryable until the next refresh. This is acceptable: new episodes are rare and the previous n episodes are what matters for context.

Using `format_episode_for_embedding()` (already defined, with its own tests) ensures the text encoding decision is made once and shared between the embedder and any future consumer.

## Consequences
- `COLLECTIONS` grows from 5 to 6. All consumers of `COLLECTIONS` iterate over it; adding an entry is safe.
- `ChromaKnowledgeStore` creates a `repair_history` collection on first instantiation after this change — no migration needed for existing ChromaDB instances.
- After the next `--mode rag-refresh`, `repair_history` will contain one chunk per previously-run episode. `_pre_inject_knowledge` will surface them when cosine similarity exceeds 0.35.
- The `embedded_at` column in `repair_episodes` is now written as intended.
- ADR 014 is considered implemented: the pre-injection path is complete and functional.

## Related decisions
- [ADR 014 — Episodic context injection](014-episodic-context-injection.md): this ADR completes the implementation described there.
- [ADR 008 — External resolution detection](008-external-resolution-detection.md): injected episodes don't interfere with self-resolution detection; the loop's external-resolution check fires normally.
- [ADR 013 — Unified agent methodology](013-prompt-externalization.md): `repair_history` chunks surface as "Relevant context" blocks the same way runbook and concept chunks do; no prompt change needed.
