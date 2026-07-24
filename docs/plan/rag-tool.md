# RAG Knowledge Layer

Part of the [Roadmap](../roadmap.md) · Milestone 2 (redesigned as Phase 14).

---

### Problem

The agent's knowledge of HA breaking changes, HACS integration deprecations, and community repair patterns is limited to what the local model was trained on. Model training cutoffs mean recent HA releases, new deprecations, and community-discovered fixes are invisible to the agent at inference time.

---

### Design (updated from original roadmap spec)

Originally planned as a `[KNOWLEDGE]` block injected into a fixed system prompt. Redesigned as a `query_knowledge` tool registered in the tool-calling loop (Milestone 6 / Phase 13), so the agent queries for relevant context only when it judges it useful. This avoids wasting tokens on irrelevant retrieved chunks and composes naturally with the loop architecture.

**Prerequisite:** Phase 13 (tool registry) must be complete. The `query_knowledge` slot is reserved in the registry from item 35; implementation arrives here.

---

### Components

**Vector store:** ChromaDB, running locally on macOS. Docker is simplest; native install also viable. Path configured via `CHROMADB_PATH`.

**Embedding model:** `nomic-embed-text` via Ollama — zero WAN, already running locally, consistent with the 0-WAN-during-fix-cycles constraint.

**Collections:**

| Collection | Source | Refresh |
|------------|--------|---------|
| `ha_release_notes` | HA core release pages (breaking changes sections) | Weekly |
| `hacs_changelogs` | HACS component changelogs for installed integrations | Weekly |
| `community_cases` | Merged anonymized repair episodes from the Federated Case Library | Weekly (populated by Milestone 9 / Phase 17; collection exists but empty until then) |

**Tool (implements reserved slot from item 35):**

| Tool | Description |
|------|-------------|
| `query_knowledge` | Semantic search across all collections; returns top-K ranked chunks with source and collection metadata |

---

### Config Keys

| Key | Default | Meaning |
|-----|---------|---------|
| `CHROMADB_PATH` | `./chromadb/` | Local vector store directory |
| `RAG_TOP_K` | 5 | Chunks returned per query |
| `RAG_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model name |

---

### Phase Deliverables

| Item | Description |
|------|-------------|
| 42 | ChromaDB setup + `nomic-embed-text` embedding via Ollama; collection schema and client wrapper |
| 43 | HA release notes scraper: fetch release pages, parse breaking-changes sections, chunk, embed, upsert |
| 44 | HACS changelog scraper; `query_knowledge` tool implementation registered in tool registry |
| 45 | Weekly refresh via macOS `launchd` plist; vector store maintenance (prune stale chunks) |

---

### Done when

- Agent correctly cites a specific HA breaking change from the local vector DB with zero WAN calls
- `query_knowledge` tool is registered and tested with a fake ChromaDB client; no real ChromaDB in unit suite
- Both scrapers run end-to-end and upsert chunks to their collections
- Weekly `launchd` job fires correctly on schedule
- `community_cases` collection exists and is queryable (empty until Phase 17 delivers cases)
