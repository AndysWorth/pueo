# ADR 029 — Hybrid dense+sparse retrieval in ChromaKnowledgeStore

## Status
Accepted

## Context
Pueo's `query_knowledge` tool previously used cosine similarity (dense retrieval) as its only ranking signal. Dense retrieval works well when the query and the document share semantic meaning, but it fails on exact lexical matches — particularly relevant for Home Assistant queries that include specific YAML keys (`mqtt:`, `zha:`, `recorder:`), service call names (`homeassistant.reload_config_entry`), or exact error strings that appear verbatim in the knowledge base.

For example, a query for `"mqtt broker unavailable"` would correctly retrieve a conceptually similar chunk about broker connectivity, but a query for `"recorder.purge_keep_days"` might miss an integration-doc chunk that contains the exact field name because the embedding space smooths over surface form.

BM25 (Best Match 25), the standard sparse retrieval algorithm, addresses this gap. It scores documents by keyword overlap using term frequency and inverse document frequency, making it effective for exact-match retrieval. The tradeoff is that BM25 ignores semantic similarity entirely — a document that describes the same concept using different words scores zero.

Hybrid retrieval combines both: dense embeddings handle semantic paraphrase, BM25 handles exact-match recall.

## Decision
Add hybrid BM25+cosine retrieval to `ChromaKnowledgeStore`.

### Implementation

**Dependency**: `rank-bm25` (pure Python, no C extension, ~5 KB) added to `requirements.txt`.

**In-memory BM25 index**: `ChromaKnowledgeStore` maintains one `BM25Okapi` instance per collection (`_bm25_index`) and a corresponding list of document IDs (`_bm25_ids`). The index is invalidated on every `upsert()` call and rebuilt lazily on the next `query()` via `_rebuild_bm25()`, which fetches all documents from ChromaDB and tokenizes them.

**Score blending**: `query()` fetches cosine similarities from ChromaDB as before, then retrieves BM25 scores for the same collection using `_bm25_scores_for_collection()`. BM25 scores are normalised to [0, 1] by dividing by the maximum raw score. The final `score` for each chunk is:

```
score = cosine_sim × (1 − hybrid_weight) + bm25_sim × hybrid_weight
```

The `score` field on `KnowledgeChunk` now reflects the blended value. The existing authority-ranked sort `(authority × 0.3) + (score × 0.7)` continues to apply on top of this.

**Configuration**: new `RAG_HYBRID_WEIGHT` key (default 0.3, range [0.0, 1.0]):
- `0.0` → pure cosine (previous behavior)
- `1.0` → pure BM25
- `0.3` (default) → 70% cosine + 30% BM25

Follows the triple-update rule: `config.py`, `config.yaml.default`, `setup.sh`.

**`FakeKnowledgeStore`** is unchanged. Its keyword-in-text match is already a degenerate form of sparse retrieval sufficient for unit tests. BM25 is tested separately against the `ChromaKnowledgeStore` internals.

### Construction
`ChromaKnowledgeStore` gains a `hybrid_weight` constructor parameter. All three construction sites in `main.py` and `agents/ha_agent_sandbox_engine.py` pass `hybrid_weight=config.RAG_HYBRID_WEIGHT`.

## Rationale

**BM25 catches what cosine misses.** Home Assistant YAML has a stable, sparse vocabulary of field names and service identifiers. When a user asks `query_knowledge("recorder purge_keep_days field")`, BM25 scores the chunk containing that exact phrase at near-maximum while cosine may score a generic `recorder` concept chunk higher. Blending ensures both signals contribute.

**Pure Python, no build complexity.** `rank-bm25` has no C extension, no native dependencies, and no platform-specific wheel. It installs identically on macOS ARM, Linux x86, and Docker — consistent with Pueo's zero-friction installation goal.

**Lazy rebuild amortises cost.** Rebuilding the BM25 index over 1,000–5,000 documents takes ~10 ms. Rebuilding on every upsert call would be wasteful during a RAG refresh that upserts thousands of documents; invalidating and rebuilding lazily on the first subsequent query means the index is always fresh but never rebuilt more than once per query batch.

**Default weight 0.3 is conservative.** Cosine similarity is the primary ranking signal (0.7 weight); BM25 is a tiebreaker that boosts exact-match documents. This preserves semantic ranking quality while recovering exact-match recall.

## Consequences

- `rank-bm25` is added to `requirements.txt`.
- `ChromaKnowledgeStore.query()` is slightly more expensive when `hybrid_weight > 0`: one ChromaDB `get()` per collection on the first query after an upsert (to build the BM25 index), plus a BM25 scoring pass per query. For Pueo's collection sizes (~1,000–5,000 docs) this is sub-millisecond.
- The `KnowledgeChunk.score` field now reflects a blended score, not raw cosine similarity. Code that interprets `score` as a cosine similarity directly (there is none in production) would need to be updated; `min_score` filtering continues to work correctly since blended scores are also in [0, 1].
- When `rank-bm25` is not installed, `_rebuild_bm25()` silently returns without building the index and `query()` falls back to cosine-only retrieval.
- `FakeKnowledgeStore` is unaffected; test suites that exercise the fake store require no changes.
- New unit tests cover: BM25 index built after upsert; exact-keyword query outranks cosine-miss; `hybrid_weight=0.0` produces pure cosine behavior; normalisation when all BM25 scores are zero.

## Related decisions
- [ADR 030 — Authority-ranked knowledge context assembly](030-authority-ranked-knowledge.md): authority ranking applies after hybrid blending; the two compose correctly — `score` used in the authority sort is now the blended score.
- [ADR 031 — Repair episode embedding](031-repair-episode-embedding.md): `repair_history` chunks benefit from hybrid retrieval; exact error strings from past repairs will rank higher when the same error recurs.
- [ADR 011 — HA live lookup](011-ha-live-lookup.md): `fetch_ha_docs` is complementary; it fetches full source files on demand. `query_knowledge` with hybrid retrieval improves recall from pre-indexed content before `fetch_ha_docs` is needed.
