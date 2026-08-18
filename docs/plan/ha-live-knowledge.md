# Milestone 12 — Agent Self-Knowledge + HA Live Lookup

## Objective

Give Pueo two complementary knowledge capabilities:

1. **Self-knowledge**: The repair and NetAlertX agents can inspect their own tool registry and pipeline code (`read_source`) so the LLM can reason about available capabilities before choosing an action.
2. **HA live lookup**: All agents can fetch Home Assistant component source files on demand (`fetch_ha_docs`), with pre-populated cache for installed integrations so local mode requires no WAN traffic during inference.

## Why here

Milestones 6 (tool loop), 6.6 (conversational agent), and 11 (transparency) established Pueo as an LLM-reasoned system. Self-knowledge and HA live lookup are natural extensions: a reasoning agent should know what it can do, and it should be able to look up the HA source when encountering an unfamiliar integration or an underdocumented config field.

ADR 010 (self-awareness) and ADR 011 (HA live lookup) document the design rationale. This file specifies implementation.

---

## Task list

### A. `read_source` in all registries

**File:** `utils/tool_registry.py`

Add `READ_SOURCE` to `build_ha_tool_registry()` (after `FINISH_REPAIR`) and to `build_netalertx_tool_registry()` (after `FINISH_REPAIR`). No other change — the ToolDefinition and executor method already exist.

```python
# build_ha_tool_registry() — add one line:
registry.register(READ_SOURCE)

# build_netalertx_tool_registry() — add one line:
registry.register(READ_SOURCE)
```

**System prompt update:** `utils/agent_loop.py` `_AGENT_LOOP_SYSTEM_PROMPT`

Append after the `TYPICAL FLOW` line:
```
SELF-KNOWLEDGE: Call read_source("utils/tool_registry.py") to see which tools are
registered in this session. Call fetch_ha_docs(domain, filename) for HA component
source or docs when query_knowledge returns insufficient context.
```

---

### B. `fetch_ha_docs` tool

#### B1. ToolDefinition — `utils/tool_registry.py`

```python
FETCH_HA_DOCS = ToolDefinition(
    name="fetch_ha_docs",
    description=(
        "Fetch a Home Assistant component file from GitHub (raw). "
        "Returns the file content. In local LLM mode, serves from cache only — "
        "returns an error on cache miss. In cloud/both mode, fetches live and caches. "
        "Use query_knowledge first for release-note context; use this for source-level detail."
    ),
    parameters={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "HA integration domain, e.g. 'zha', 'mqtt', 'recorder'",
            },
            "filename": {
                "type": "string",
                "description": (
                    "File to fetch. Allowed: __init__.py, manifest.json, "
                    "config_flow.py, const.py, strings.json, or any *.md file."
                ),
            },
        },
        "required": ["domain", "filename"],
    },
)
```

Register in:
- `build_ha_tool_registry()` — after `READ_SOURCE`
- `build_chat_tool_registry()` — after `READ_SOURCE`
- `build_netalertx_tool_registry()` — after `READ_SOURCE`

#### B2. Executor method — `utils/tool_executor.py`

```python
_HA_SOURCE_ALLOWED_FILES = frozenset({
    "__init__.py", "manifest.json", "config_flow.py",
    "const.py", "strings.json",
})
_HA_SOURCE_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/dev"
    "/homeassistant/components/{domain}/{filename}"
)

async def _fetch_ha_docs(self, domain: str, filename: str) -> str:
    # Validate filename — allowlist + *.md
    if filename not in _HA_SOURCE_ALLOWED_FILES and not filename.endswith(".md"):
        raise ToolError(f"filename '{filename}' is not allowed")
    if "/" in domain or "/" in filename:
        raise ToolError("path traversal not allowed")

    cache_dir = Path(_config.HA_SOURCE_CACHE_DIR) / domain
    cache_file = cache_dir / filename

    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")[:8000]

    # Local mode: cache miss is a hard stop — no WAN
    if _config.LLM_PROVIDER == "local":
        raise ToolError(
            f"'{domain}/{filename}' not in cache. "
            "Run the RAG refresh to pre-cache installed integrations, "
            "or use query_knowledge for release-note context."
        )

    # Cloud/both mode: fetch live, cache result
    import httpx  # lazy import — only needed in cloud/both mode
    url = _HA_SOURCE_URL.format(domain=domain, filename=filename)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"User-Agent": "pueo-rag/1.0"})
    if resp.status_code == 404:
        raise ToolError(f"HA component '{domain}/{filename}' not found on GitHub")
    resp.raise_for_status()
    content = resp.text[:8000]

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(content, encoding="utf-8")
    return content
```

Add `"fetch_ha_docs": self._fetch_ha_docs` to the `execute()` dispatch table.

#### B3. Config — triple-update rule

**`config.py`**
```python
HA_SOURCE_CACHE_DIR: str = cfg.get("ha_source_cache_dir", ".cache/ha_source")
```

**`config.yaml.default`**
```yaml
# Cache directory for pre-fetched HA component source files
ha_source_cache_dir: .cache/ha_source
```

**`setup.sh`**
No interactive prompt needed (sensible default). Add a `mkdir -p` in the setup section that creates `.cache/ha_source`.

---

### C. RAG refresh pre-population — `utils/ha_docs_scraper.py`

Add `prefetch_installed_integration_sources(ha_url, ha_token, cache_dir)`:

1. Call `discover_installed_integrations(ha_url, ha_token)` (already exists) to get domain list
2. For each domain, fetch `__init__.py`, `manifest.json`, `const.py` from GitHub raw (same URL pattern as B2)
3. Write each file to `{cache_dir}/{domain}/{filename}`, skipping if already cached and file is < 7 days old
4. Return a summary dict: `{domain: ["ok", "ok", "404"]}` for logging

Call this function from the main RAG refresh cycle in `main.py` (or wherever `scrape_cached_release_notes` is called), guarded by: skip if `LLM_PROVIDER == "local"` AND user has not opted into a scheduled WAN refresh (default: run at refresh time, controlled by the existing RAG refresh schedule).

---

### D. Tests

**`tests/test_tool_executor.py`** — add:
- `test_fetch_ha_docs_cache_hit`: cache file exists → returns content, no HTTP call
- `test_fetch_ha_docs_local_cache_miss`: `LLM_PROVIDER=local`, no cache file → raises `ToolError` containing "not in cache"
- `test_fetch_ha_docs_cloud_live_fetch`: `LLM_PROVIDER=cloud`, no cache → mocked `httpx.AsyncClient` returns 200 → content returned and written to cache
- `test_fetch_ha_docs_disallowed_filename`: `filename="secrets.yaml"` → raises `ToolError`
- `test_fetch_ha_docs_path_traversal`: `domain="../etc"` → raises `ToolError`

**`tests/test_tool_registry.py`** — add:
- `test_ha_tool_registry_includes_read_source`
- `test_ha_tool_registry_includes_fetch_ha_docs`
- `test_netalertx_tool_registry_includes_read_source`

---

## Validation gate

1. **Chat session**: `"Show me the ZHA integration manifest"` → agent calls `fetch_ha_docs(domain='zha', filename='manifest.json')` → returns JSON content
2. **Repair session**: Unknown integration in config → agent calls `read_source("utils/tool_registry.py")` and sees `fetch_ha_docs` in the list → calls it
3. **Local WAN gate**: `LLM_PROVIDER=local` + empty cache → `fetch_ha_docs` raises `ToolError`, no HTTP call fired (verified in unit test)
4. **RAG pre-population**: After a RAG refresh with `LLM_PROVIDER=cloud`, `.cache/ha_source/` contains subdirectories for installed integrations
5. **CI gate**: `pytest --cov --cov-fail-under=90 --ignore=tests/integration` passes; `mypy` passes on new code

---

## Related files

| File | Change |
|---|---|
| `utils/tool_registry.py` | Add `FETCH_HA_DOCS` ToolDefinition; add to 3 registries; add `READ_SOURCE` to 2 registries |
| `utils/tool_executor.py` | Add `_fetch_ha_docs()` method + dispatch entry; add `_HA_SOURCE_ALLOWED_FILES`, `_HA_SOURCE_URL` |
| `utils/agent_loop.py` | Extend `_AGENT_LOOP_SYSTEM_PROMPT` with self-knowledge note |
| `utils/ha_docs_scraper.py` | Add `prefetch_installed_integration_sources()` |
| `config.py` | Add `HA_SOURCE_CACHE_DIR` |
| `config.yaml.default` | Add `ha_source_cache_dir` |
| `setup.sh` | Add `.cache/ha_source` mkdir |
| `tests/test_tool_executor.py` | 5 new tests for `fetch_ha_docs` |
| `tests/test_tool_registry.py` | 3 new tests for registry membership |
| `docs/decisions/010-agent-self-awareness.md` | ADR (new) |
| `docs/decisions/011-ha-live-lookup.md` | ADR (new) |
| `docs/roadmap.md` | New milestone row + narrative |
| `CLAUDE.md` | New Key Patterns entries |
