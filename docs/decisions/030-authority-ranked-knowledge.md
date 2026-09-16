# ADR 030 — Authority-ranked knowledge context assembly

## Status
Accepted

## Context
Pueo's `query_knowledge` tool queries six ChromaDB collections (concepts, strategies, ha_docs, repair_history, community_cases, ha_release_notes) and returns the top-K chunks ranked by cosine similarity alone. This works for the common case, but two deficiencies appeared as the knowledge base grew:

1. **Source authority is invisible.** A chunk from HA's official documentation and a candidate runbook (unreviewed, LLM-proposed) were presented identically. The model had no signal about which source to trust when they conflicted.

2. **Version context is absent.** Release notes and scraper-fetched integration docs are version-specific. A chunk from 2024.1 release notes describing a breaking change in `recorder` is less relevant to an HA 2025.11 installation than a chunk from 2025.10 notes. Without version metadata, the query path could not surface this signal even if the ranking algorithm wanted to use it.

Both gaps increased the risk of the model applying an outdated fix or citing an unreviewed runbook as authoritative.

## Decision

### 1. Authority scores on `KnowledgeChunk`

Add `authority_score: float = 0.0` to `KnowledgeChunk`. The score is assigned by `_authority_score(collection, metadata)` at query time, with the following tiers:

| Source | Score |
|---|---|
| `ha_docs` (HA component source) | 1.0 |
| `ha_concepts` (HA official docs) | 0.95 |
| `ha_release_notes` (HA changelog) | 0.90 |
| `strategies` seed runbook | 0.85 |
| `repair_history` (successful past repairs) | 0.70 |
| `strategies` reviewed community runbook | 0.60 |
| `community_cases` (federated cases) | 0.55 |
| `strategies` candidate runbook (unreviewed) | 0.30 |

The tiers reflect how much to trust the source independent of semantic similarity: official HA source is highest; unreviewed LLM proposals are lowest. Seed runbooks (human-curated) rank above community runbooks (externally sourced); successful repair history ranks above unreviewed proposals because its quality is evidenced by a positive outcome.

### 2. Blended relevance sort

Both `.query()` callers (the default multi-collection path and the single-collection path) sort results by:

```
blended_score = (authority_score × 0.3) + (cosine_similarity × 0.7)
```

Cosine similarity retains dominant weight (0.7) so the ranking remains semantically driven. Authority acts as a 30% tiebreaker that consistently surfaces official docs over unreviewed speculation when similarity is comparable, without burying a highly-relevant candidate runbook behind a weakly-relevant official doc.

The weights were chosen to be simple and auditable — not tuned on eval data. Revisit if evals show authority is over- or under-weighted.

### 3. Authority labels in `query_knowledge` output

`_query_knowledge` in `utils/agent/tool_executor.py` prepends a short label to each chunk, so the model can see source authority without reading metadata fields:

| Chunk source | Label |
|---|---|
| HA official docs / concepts | `[OFFICIAL]` |
| Release notes | `[OFFICIAL]` |
| Seed runbook | `[SEED RUNBOOK]` |
| Candidate runbook | `[CANDIDATE RUNBOOK – unreviewed]` |
| Community-reviewed runbook | `[COMMUNITY RUNBOOK]` |
| Repair history | `[PAST REPAIR]` |
| Community cases | `[COMMUNITY]` |

The labels are applied by `_knowledge_authority_label(chunk)` — a static method that reads the collection and the `runbook_state` metadata field. Labels appear at the start of the chunk text in the tool response, making them visible in the model's context without requiring it to inspect JSON metadata.

### 4. Version range metadata on knowledge chunks

Scrapers that produce version-specific content now embed `ha_version_min` and `ha_version_max` metadata fields:

- `ha_release_notes_scraper.py`: sets both fields equal to the release version string for each chunk (e.g. `"2025.11.0"`).
- `ha_docs_scraper.py`, `ha_concepts_scraper.py`, `hacs_scraper.py`: accept an optional `scraped_for_ha_version` parameter; when provided, set both fields to that version. `main.py::run_rag_refresh` loads the live HA version from `load_environment_profile()` and passes it to all four embed calls.
- `kb_ingester.py`: propagates `ha_version_min`/`ha_version_max` from `ManifestEntry` into ChromaDB metadata when the manifest specifies a version range for a runbook.

Version metadata is advisory in this ADR — it is stored in chunk metadata and visible to future ranking logic (Session 9, version score boosting) but not yet used to modify the blended sort score.

## Rationale

**Transparency for the model.** Text labels are simpler and more reliable than metadata inspection. The model can reason "this is an unreviewed candidate; verify before applying" from a `[CANDIDATE RUNBOOK – unreviewed]` label without needing access to the full metadata JSON.

**30/70 weight split.** A higher authority weight would cause high-authority but low-similarity results to surface (e.g., generic HA docs for a specific Zigbee error). A lower weight would make authority invisible. 0.3/0.7 ensures the ranking stays semantically driven while giving official sources a consistent edge when similarity is close.

**Version metadata now, boosting later.** Adding the metadata fields in this session costs nothing and unblocks Session 9 (version score boosting). Doing the boosting in two steps — metadata first, ranking second — keeps each change reviewable in isolation.

## Consequences

- `KnowledgeChunk` gains `authority_score: float = 0.0` — a non-breaking addition; existing callers that don't read the field are unaffected.
- `ChromaKnowledgeStore.query()` output ordering changes. Sessions seeing blended sorting may return different top-K results than before. Evals cover this: `TestAuthorityBlendedSorting` verifies official chunks beat unreviewed runbooks at equivalent similarity.
- All four scrapers gain an optional `scraped_for_ha_version` parameter with default `None`. When `None`, no version metadata is written — backwards-compatible with existing cached data.
- The `query_knowledge` tool response now contains text labels. Prompts that parse the output programmatically would need updating; no Pueo code does this — the model reads it as natural language.
- ADR 018 (Unified Agent Methodology) is unaffected: the 6-phase cycle calls `query_knowledge`; this ADR only changes what that call returns, not when it is called.

## Related decisions
- [ADR 018 — Unified Agent Methodology](018-unified-agent-methodology.md): `query_knowledge` is Phase 1 of every investigation. Authority labels make Phase 1 output richer without changing the phase structure.
- [ADR 031 — Repair episode embedding](031-repair-episode-embedding.md): `repair_history` chunks now carry authority score 0.70 and a `[PAST REPAIR]` label — higher than unreviewed runbooks, lower than official docs.
- [ADR 013 — Prompt externalization](013-prompt-externalization.md): authority labels appear in the tool response text read by `agent_loop.md`; no prompt change is needed.
