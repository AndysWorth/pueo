# ADR 011 — On-demand HA documentation and source code lookup

## Status
Accepted

## Context
Pueo needs accurate knowledge of Home Assistant internals to diagnose config errors, interpret log messages, and propose valid fixes. Currently this knowledge comes from two sources:

1. **Periodic RAG scraping** — `ha_release_notes_scraper.py`, `ha_blog_scraper.py`, `ha_docs_scraper.py`, and `hacs_scraper.py` fetch release notes, blog posts, integration documentation, and HACS changelogs at refresh time. Results are embedded into ChromaDB and queried via the `query_knowledge` tool.

2. **HA environment profile** — `get_ha_profile` returns a cached snapshot of the live HA instance: Core version, OS version, installed integrations, config keys.

Two gaps remain:

- **HA component source code** is not fetched at all. Docs describe the public API; source code shows the actual implementation — particularly important when a component behaves differently from its documentation, or when a config key's valid values are defined only in `const.py`.
- **Uncached integrations** are not covered. The RAG pipeline only indexes integrations discovered at the last refresh. A newly installed integration, or an integration not yet scraped, returns no `query_knowledge` results.

Both gaps mean the agent sometimes reasons from incomplete information, increasing the risk of a bad fix or a missed diagnosis.

## Decision
Add a `fetch_ha_docs(domain, filename)` tool that gives the agent on-demand access to HA component files from the GitHub repository.

### Tool contract
- **Input**: `domain` (e.g. `"zha"`, `"mqtt"`) and `filename` (e.g. `"__init__.py"`, `"manifest.json"`, `"const.py"`)
- **Allowed filenames**: any filename that does not contain `/` or `..`; responses are truncated to 16,000 characters. The URL structure itself (public GitHub raw endpoint for `home-assistant/core`) is the security boundary; path-traversal guards on domain and filename enforce it. An explicit filename allowlist provided no additional security but blocked the most useful files (`sensor.py`, `switch.py`, etc.) and was removed.
- **Source URL pattern**: `https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/{domain}/{filename}`
- **Cache path**: `{HA_SOURCE_CACHE_DIR}/{domain}/{filename}` (default `.cache/ha_source/`)
- **WAN gate**: In `LLM_PROVIDER=local` mode, only serve from cache. On a cache miss, return a `ToolError` directing the agent to use `query_knowledge` instead. Never make a network call. In `cloud` or `both` mode, fetch live on a cache miss, write the result to cache, and return the content.

### RAG refresh integration
At every RAG refresh cycle, `ha_docs_scraper.py` also pre-fetches source files for all currently installed integrations (discovered via `GET /api/states`). The files fetched are: `__init__.py`, `manifest.json`, `const.py`. This ensures local mode has source coverage for the user's actual setup without requiring any WAN traffic during inference.

### Config
New key: `HA_SOURCE_CACHE_DIR` (default `.cache/ha_source`). Follows the triple-update rule: `config.py` + `config.yaml.default` + `setup.sh`.

### Registry
`fetch_ha_docs` is registered in:
- `build_ha_tool_registry()` — repair agent
- `build_chat_tool_registry()` — conversational agent
- `build_netalertx_tool_registry()` — NetAlertX healer

## Rationale
The 0-WAN constraint for `LLM_PROVIDER=local` is a privacy-first design invariant (see evaluation matrix in `docs/roadmap.md`). The cache-only local-mode behavior preserves this: the agent can call `fetch_ha_docs` freely without worrying about mode, and the tool itself enforces the policy. The pre-population of installed integrations at refresh time means the cache miss path is the exception, not the rule.

Fetching from `raw.githubusercontent.com` (not a live HA API) means the tool has no dependency on the running HA instance — it works even during a repair that has temporarily halted the HA core.

## Consequences
- New tool `fetch_ha_docs` in `utils/tool_executor.py` (`_fetch_ha_docs` method) and `ToolDefinition FETCH_HA_DOCS` in `utils/tool_registry.py`
- `ha_docs_scraper.py` extended to pre-fetch `__init__.py`, `manifest.json`, `const.py`, `sensor.py`, `binary_sensor.py`, `switch.py`, `light.py`, `cover.py`, `climate.py`, `media_player.py`, `number.py`, `select.py` for installed integrations (404s silently skipped)
- `config.py`, `config.yaml.default`, `setup.sh` gain `HA_SOURCE_CACHE_DIR`
- Three registry factory functions each gain one line
- `_AGENT_LOOP_SYSTEM_PROMPT` gains a 2-line note about `fetch_ha_docs`
- WAN gate: unit tests mock the HTTP client; a WAN-gate test confirms no network call fires in local mode on cache miss
- No change to the existing `query_knowledge` flow — `fetch_ha_docs` is a complement, not a replacement

## Architectural principle
Every significant Pueo action — repair, update, cleanup, notification triage — should flow through LLM tool-calling reasoning. Infrastructure actions that bypass the LLM (scraper runs, disk-space enforcement, backup retention sweeps) are scheduled housekeeping, not decisions. When a Pueo function changes HA state, it belongs in an agent loop, not in a direct call. This principle is not yet stated explicitly in `CLAUDE.md`; it should be added as a Key Pattern entry.

## Related decisions
- [ADR 006 — LLM provider abstraction](006-llm-provider-abstraction.md): The WAN gate reads `LLM_PROVIDER` from config; the tool delegates the policy check to a single helper rather than inlining it.
- [ADR 010 — Agent self-awareness](010-agent-self-awareness.md): Self-awareness (Pueo codebase) and HA live lookup (HA codebase) are complementary capabilities delivered together in Milestone 12.
- [ADR 002 — Safety invariant](002-safety-invariant.md): `fetch_ha_docs` is read-only; it never triggers the backup chain.
