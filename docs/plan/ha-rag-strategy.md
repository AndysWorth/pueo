# HA RAG Strategy — Improved Knowledge Layer

Part of the [Roadmap](../roadmap.md) · Phase 22 (items 87–93).

---

### Problem

The existing RAG layer (Phase 15, items 49–52) was built around the assumption that the GitHub releases API `body` field contains the HA changelog. This is false for monthly GA releases: HA sets the GitHub `body` to a blog URL only (`https://www.home-assistant.io/blog/...`). The real changelog — breaking changes, migration notes, new integrations — lives on the HA blog. The current code caches this 60-byte stub as a `.txt` file and, during update analysis, invokes the LLM on near-empty input. The result is hallucinated breaking changes in the HITL update card.

Five specific gaps:

1. **Blog stub problem** (`ha_update_manager.py`, `ha_release_notes_scraper.py`): GA release notes are 60-byte URL stubs. Three files already affected: `2026.2.0.txt`, `2026.7.0.txt`, `2026.8.0.txt`.
2. **No blog scraper**: the real content (Backward Incompatible Changes section, per-integration notes, new integrations) is on the blog and is never fetched.
3. **No environment profile**: Pueo does not know which integrations are installed, which HA version is running, or what's in `configuration.yaml` as structured data. Knowledge retrieval is untargeted.
4. **Knowledge store not wired in supervisor mode**: `supervisor_main()` creates `ToolExecutor` without a `knowledge_store`. Every `query_knowledge` call from the chat agent returns "Knowledge store not configured."
5. **No query filtering**: `query_knowledge` takes only a free-text string. ChromaDB's `where` clause filtering by metadata is never used, so irrelevant chunks compete equally with relevant ones.

---

### Architecture

**Data sources used by this phase:**

| Source | URL | What we get |
|---|---|---|
| GitHub releases API (existing) | `repos/home-assistant/core/releases` | Point release changelogs (real content); GA stubs (blog URL only) |
| HA blog (new) | `https://www.home-assistant.io/blog/...` | Monthly GA changelogs — the URL is embedded in the stub |
| HA `/api/config` REST (existing, rag-refresh only) | local HA instance | `components` list → installed integration domains |
| HA WebSocket `config/config_entries/all` (new) | local HA instance | UI-configured integrations with title + state |
| HACS discovery (existing) | local HA instance | Custom components |

**Environment profile** (`utils/ha_environment.py`, new): a `HAEnvironmentProfile` dataclass persisted in SQLite that answers "what is installed on this HA instance right now." Built from the above REST/WS calls plus SSH `ha core info` / `ha os info`. Refreshed on supervisor startup and every 24 hours.

**Metadata enrichment**: ChromaDB chunks gain `release_type`, `category`, and `impacted_integration` fields so the agent can filter to relevant content.

**Knowledge store in supervisor mode**: `supervisor_main()` instantiates `ChromaKnowledgeStore` on startup (gracefully degrading if ChromaDB path missing), wiring RAG into all tool loops including the chat agent.

**Implementation order**: D (supervisor wiring, bug fix) → A (stub fix) → B (blog scraper) → C (environment profile) → E (metadata enrichment). Phase D is the only bug fix; A–E add capability.

---

### Phase Deliverables

| Item | Phase | Description |
|------|-------|-------------|
| 87 | A | Stub-body fix: beta fallback in `_fetch_github_release_notes`; neutral advisory in `analyze_breaking_changes`; stub sentinel in `fetch_ha_release_notes` |
| 88 | B | `utils/ha_blog_scraper.py`: `fetch_blog_post`, `extract_blog_url_from_stub`, `fetch_blog_release_notes`; hooked into `run_rag_refresh` |
| 89 | B | Enriched chunk metadata: `release_type`, `category`, `impacted_integration`; `where` clause support in `KnowledgeStore.query`; `integration_filter` parameter in `query_knowledge` tool |
| 90 | C | `HAEnvironmentProfile` dataclass + `build_environment_profile`; `get_config_entries` WS method; DB migration v14; save/load helpers |
| 91 | C | Wire profile into supervisor; `get_ha_profile` chat tool; use profile in `analyze_breaking_changes` and `request_update_approval` |
| 92 | D | Wire `ChromaKnowledgeStore` into `supervisor_main()` so `query_knowledge` is functional in production |
| 93 | E | HACS version metadata; HA docs `is_installed` flag; `release_type` on all bulk-fetched chunks; remove empty `community_cases` collection |

---

### Item 87 — Stub-Body Fix + Beta Fallback

**Files:** `ha_update_manager.py`, `utils/ha_release_notes_scraper.py`, `tests/test_core_agent.py`

**Stub detection threshold:** `len(body.strip()) < 500`

**`_fetch_github_release_notes(version)` in `ha_update_manager.py`:**
After fetching the tag release body, if the body is a stub, enumerate beta tags for the same minor version (`{major}.{minor}.0b5` down to `b0`), fetching each until one has real content (≥ 500 chars). If all betas are also stubs, return the stub body as-is.

```python
async def _fetch_github_release_notes(version: str) -> str:
    url = _GITHUB_API_URL.format(version=version)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers={"Accept": "application/vnd.github.v3+json"})
        resp.raise_for_status()
        body = resp.json().get("body") or ""
    if len(body.strip()) >= 500:
        return body
    # Stub: try beta tags for the same minor version
    parts = version.split(".")
    if len(parts) >= 2:
        minor_base = f"{parts[0]}.{parts[1]}.0"
        for beta in range(5, -1, -1):
            beta_tag = f"{minor_base}b{beta}"
            beta_url = _GITHUB_API_URL.format(version=beta_tag)
            async with httpx.AsyncClient(timeout=30.0) as c:
                try:
                    r = await c.get(beta_url, headers={"Accept": "application/vnd.github.v3+json"})
                    r.raise_for_status()
                    beta_body = r.json().get("body") or ""
                    if len(beta_body.strip()) >= 500:
                        return beta_body
                except Exception:
                    continue
    return body  # return stub; caller must handle
```

**`analyze_breaking_changes()` in `ha_update_manager.py`:**
Before invoking the LLM, check if `len(release_notes.strip()) < 500`. If so, extract any URL from the text and return a neutral `UpdateReadinessReport`:

```python
if len(release_notes.strip()) < 500:
    url_match = re.search(r"https?://\S+", release_notes)
    url_hint = url_match.group(0) if url_match else "https://www.home-assistant.io/blog/"
    return UpdateReadinessReport(
        target_version=update_status.latest_version,
        safe_to_update=True,
        breaking_changes=[],
        affected_config_keys=[],
        pueo_command_risks=[],
        recommendation=(
            f"Release notes for {update_status.latest_version} are not yet available "
            f"on GitHub. Review manually: {url_hint}"
        ),
    )
```

**`fetch_ha_release_notes()` in `utils/ha_release_notes_scraper.py`:**
After writing a release file, if the body is a stub, write it as `STUB:{body}` so `chunk_release_notes` can detect and skip it.

**`chunk_release_notes()` in `utils/ha_release_notes_scraper.py`:**
If `release_notes.startswith("STUB:")`, return empty lists `([], [], [])` — the scraper skips embedding.

**`scrape_cached_release_notes()` in `utils/ha_release_notes_scraper.py`:**
Skip files whose content starts with `STUB:`.

**Operational steps (same PR):**
Delete `2026.2.0.txt`, `2026.7.0.txt`, `2026.8.0.txt` from `.cache/ha_release_notes/` so they are re-fetched with the fallback logic. Document this in the PR description.

**Tests** (add to `TestFetchReleaseNotesCached` in `tests/test_core_agent.py`):
- `test_stub_triggers_beta_fallback` — injected fetcher returns stub for GA, real content for `b3`; assert returned content is the beta body
- `test_all_stubs_returns_original` — all beta fetchers return stubs; assert GA stub body is returned
- `test_neutral_advisory_on_stub_notes` — `analyze_breaking_changes` with stub input; assert `breaking_changes == []` and recommendation contains URL
- `test_stub_sentinel_written_to_cache` — `fetch_ha_release_notes` with stub body; assert file starts with `STUB:`
- `test_stub_sentinel_skipped_by_scraper` — `scrape_cached_release_notes` with `STUB:` file; assert 0 chunks upserted

---

### Item 88 — HA Blog Scraper (fetch and parse)

**Files:** `utils/ha_blog_scraper.py` (new), `main.py`, `tests/test_utils.py`

**New `utils/ha_blog_scraper.py`:**

No new dependencies — uses `urllib.request` (stdlib) and `html.parser.HTMLParser` (stdlib).

```python
class _ArticleExtractor(HTMLParser):
    """Converts the <article> body to clean Markdown-like plain text."""

    def __init__(self):
        super().__init__()
        self._in_article = False
        self._depth = 0
        self._buf: list[str] = []
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        if tag == "article":
            self._in_article = True
        if not self._in_article:
            return
        self._tag_stack.append(tag)
        if tag in ("h2", "h3", "h4"):
            self._buf.append("\n\n")
            self._buf.append("## " if tag == "h2" else "### " if tag == "h3" else "#### ")
        elif tag == "p":
            self._buf.append("\n\n")
        elif tag in ("li",):
            self._buf.append("\n- ")

    def handle_endtag(self, tag):
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag == "article":
            self._in_article = False

    def handle_data(self, data):
        if self._in_article:
            text = data.strip()
            if text:
                self._buf.append(text + " ")

    def result(self) -> str:
        import re
        text = "".join(self._buf)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
```

**`fetch_blog_post(url: str, *, _fetcher=None) -> str`:**
Fetches the URL using `urllib.request` with a `User-Agent: pueo-rag-refresh/1.0` header. Passes the HTML through `_ArticleExtractor` and returns the clean text. The `_fetcher` injectable is for tests.

**`extract_blog_url_from_stub(stub_text: str) -> Optional[str]`:**
Returns the first `https://www.home-assistant.io/blog/...` URL found in the text, or `None`.

**`fetch_blog_release_notes(cache_dir: str, *, _fetcher=None) -> int`:**
Iterates `*.txt` files in `cache_dir`. For each file whose content starts with `STUB:`, extracts the blog URL (via `extract_blog_url_from_stub`), fetches it via `fetch_blog_post`, and if real content is returned (≥ 500 chars), overwrites the file with the blog content (no `STUB:` prefix). Returns the count of files successfully replaced.

**Hook into `run_rag_refresh` in `main.py`:**
After `fetch_ha_release_notes(...)`, call `fetch_blog_release_notes(cache_dir)`. This runs before `scrape_cached_release_notes`, so newly unblocked files are embedded in the same refresh run.

**Tests** (add to `tests/test_utils.py`):
- `test_extract_blog_url_from_stub` — various stub formats; assert correct URL extracted
- `test_fetch_blog_post_strips_html` — fake fetcher returns minimal HTML with `<article>` and headings; assert result is clean text with `##` markers
- `test_fetch_blog_release_notes_replaces_stub` — tmp dir with one `STUB:http://...` file; fake fetcher returns real content; assert file updated and count == 1
- `test_fetch_blog_release_notes_skips_real_files` — real content file in cache; assert not touched, count == 0

---

### Item 89 — Enriched Metadata + Query Filtering

**Files:** `utils/ha_release_notes_scraper.py`, `utils/knowledge_store.py`, `utils/tool_executor.py`, `utils/tool_registry.py`, `interfaces.py`, `tests/test_utils.py`, `tests/test_core_agent.py`

**`chunk_release_notes()` — enriched metadata:**

Add `release_type` and `category` to each chunk's metadata. `release_type` is derived from the version tag:
- `"ga"` — tag matches `YYYY.M.0` with no suffix (e.g. `2026.8.0`)
- `"patch"` — tag matches `YYYY.M.P` where P > 0 (e.g. `2026.7.2`)
- `"beta"` — tag contains `b\d+` (e.g. `2026.8.0b4`)

`category` is assigned per-section by `parse_blog_sections` (new, in `ha_blog_scraper.py`) or defaults to `"general"` for non-blog content:
- Section heading matches `backward incompatible|breaking change` (case-insensitive) → `"breaking_change"`
- Section heading matches `new integration` → `"new_integration"`  
- Section heading matches `deprecat` → `"deprecation"`
- Otherwise → `"general"`

`impacted_integration` is extracted from section headings and inline code spans within breaking-change sections. A regex `r'`([a-z][a-z0-9_]+)`'` extracts domain candidates; they are cross-referenced against the known integration list to reduce false positives. Each chunk stores the first extracted domain (or empty string if none).

Updated metadata dict per chunk:
```python
{
    "source": f"ha_release_notes/{version}",
    "version": version,
    "release_type": release_type,    # "ga" | "patch" | "beta"
    "category": category,             # "breaking_change" | "new_integration" | "deprecation" | "general"
    "impacted_integration": domain,   # "" if none detected
}
```

**`KnowledgeStoreClientProtocol.query()` — `where` parameter:**

Add optional `where: Optional[dict] = None` to the `query` method signature in `interfaces.py`, `knowledge_store.py` (`ChromaKnowledgeStore` and `FakeKnowledgeStore`). Pass through to ChromaDB's `col.query(query_texts=..., where=where)` when set.

**`query_knowledge` tool — `integration_filter` parameter:**

Update `ToolDefinition` in `utils/tool_registry.py`:
```python
ToolDefinition(
    name="query_knowledge",
    description=(
        "Query the local RAG knowledge base for HA breaking changes, integration docs, "
        "and HACS changelogs. Pass integration_filter to scope results to specific domains."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query text"},
            "integration_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of integration domains to filter results (e.g. ['zha', 'mqtt'])",
            },
        },
        "required": ["query"],
    },
)
```

Update `_query_knowledge()` in `utils/tool_executor.py` to accept `integration_filter: list[str] | None = None`. When provided and non-empty, construct:
```python
where = {"impacted_integration": {"$in": integration_filter}}
chunks = self._knowledge_store.query(query, top_k=RAG_TOP_K, where=where)
```
If no filter, `where=None` (current behavior).

**Tests:**
- `test_chunk_release_notes_release_type` — tag `2026.8.0` → `"ga"`, `2026.7.2` → `"patch"`, `2026.8.0b4` → `"beta"`
- `test_chunk_release_notes_category` — blog text with "Backward Incompatible" section → chunk has `category="breaking_change"`
- `test_knowledge_store_where_clause` — `FakeKnowledgeStore.query` with `where` kwarg filters results correctly
- `test_query_knowledge_integration_filter` — `ToolExecutor._query_knowledge` with `integration_filter=["zha"]` calls store with correct `where` dict

---

### Item 90 — HAEnvironmentProfile Dataclass

**Files:** `utils/ha_environment.py` (new), `utils/ha_ws_client.py`, `interfaces.py`, `ha_agent_advanced.py` (migration v14), `tests/test_core_agent.py`

**New `utils/ha_environment.py`:**

```python
@dataclass
class HAEnvironmentProfile:
    ha_version: str = ""
    os_version: str = ""
    supervisor_version: str = ""
    installed_integrations: list[str] = field(default_factory=list)
    hacs_integrations: list[str] = field(default_factory=list)
    config_yaml_top_keys: list[str] = field(default_factory=list)
    config_entries: list[dict] = field(default_factory=list)
    last_updated: float = 0.0
```

**`build_environment_profile(ssh_client, rest_client, ws_client, ha_token, ha_url, db_path) -> HAEnvironmentProfile`:**
Assembles the profile from all available sources. Every sub-call is wrapped in try/except; failures are logged and produce empty values rather than aborting. In order:
1. SSH `ha core info` → parse `version:` line → `ha_version`
2. SSH `ha os info` → parse `version:` and `supervisor:` lines → `os_version`, `supervisor_version`
3. `discover_installed_integrations(ha_url, ha_token)` (reuse from `ha_docs_scraper.py`) → `installed_integrations`
4. `discover_hacs_integrations(ha_url, ha_token)` (reuse from `hacs_scraper.py`) → `hacs_integrations`
5. SSH `ssh.read_file(CONFIG_REMOTE_PATH)` + `yaml.safe_load()` + `list(doc.keys())` → `config_yaml_top_keys`
6. `ws_client.get_config_entries()` → `config_entries`

**`save_environment_profile(profile, db_path)` / `load_environment_profile(db_path) -> Optional[HAEnvironmentProfile]`:**
Store/retrieve as a JSON blob in SQLite. `save` uses `INSERT OR REPLACE` with `id=1` (single-row table). `load` returns `None` if no row exists.

**DB migration v14 in `ha_agent_advanced.py`:**
```sql
CREATE TABLE IF NOT EXISTS ha_environment_profile (
    id           INTEGER PRIMARY KEY,
    profile_json TEXT NOT NULL,
    last_updated REAL NOT NULL
)
```

**`HAWebSocketClient.get_config_entries()` in `utils/ha_ws_client.py`:**
New WS message type: `{"type": "config/config_entries/all"}`. Returns the list of config-entry dicts (each has `domain`, `title`, `state`, `disabled_by`). Filters to `state == "loaded"` before returning.

**`HAWebSocketClientProtocol.get_config_entries()` in `interfaces.py`:**
Add abstract method signature.

**Tests:**
- `test_build_environment_profile_all_sources` — all fake clients return data; assert all fields populated
- `test_build_environment_profile_ssh_failure` — SSH fails; assert `ha_version == ""`, other fields still populated
- `test_save_load_round_trip` — save then load; assert equality
- `test_load_returns_none_when_empty` — fresh DB; assert `None` returned
- `test_get_config_entries_ws` — fake WS returns entry list; assert only `state=="loaded"` entries returned
- `test_migration_v14` — `ensure_db()` creates the table

---

### Item 91 — Wire Profile into Supervisor + Agent Tools

**Files:** `main.py`, `utils/tool_registry.py`, `utils/tool_executor.py`, `ha_update_manager.py`, `tests/test_core_agent.py`

**`supervisor_main()` in `main.py`:**
After creating `LoopSupervisor`, call `build_environment_profile(...)` and store the result on `sv` (or in a module-level `_ha_profile` variable accessible to the executor). Register a `profile_refresh` loop that re-calls `build_environment_profile` every `HA_PROFILE_REFRESH_HOURS` (new config key, default 24) and updates the cached value.

**New config key:** `HA_PROFILE_REFRESH_HOURS` (default `24`) in `config.py`, `config.yaml.default`, `setup.sh`.

**New `get_ha_profile` tool in `build_chat_tool_registry()`:**
```python
ToolDefinition(
    name="get_ha_profile",
    description="Return the current HA environment profile: installed version, OS version, integrations, HACS components.",
    parameters={"type": "object", "properties": {}, "required": []},
)
```

`_get_ha_profile()` in `ToolExecutor`: returns the cached `HAEnvironmentProfile` as a formatted JSON string. If no profile is cached, returns a message directing the user to restart the supervisor.

**`analyze_breaking_changes()` in `ha_update_manager.py`:**
Replace the `config_yaml_content: str` parameter with `profile: Optional[HAEnvironmentProfile] = None`. When profile is available:
- Use `profile.installed_integrations` to construct a `where` clause for the knowledge store query (pre-fetch relevant chunks before calling the LLM)
- Replace the raw `configuration.yaml` block in the LLM prompt with a structured summary: `"Installed integrations: {domains}\nHA version: {version}\nTop-level config keys: {keys}"`

When profile is `None`, fall back to the current behavior (pass raw `config_yaml_content`).

Update `request_update_approval()` in `ha_update_manager.py` to pass the cached profile to `analyze_breaking_changes`.

**Tests:**
- `test_get_ha_profile_tool_returns_profile` — executor with cached profile; assert JSON output contains integration list
- `test_analyze_breaking_changes_uses_profile` — fake LLM client; assert prompt contains structured profile summary, not raw YAML
- `TestConfigDefaults.test_ha_profile_refresh_hours_default` — assert value 24

---

### Item 92 — Wire Knowledge Store in Supervisor Mode

**Files:** `main.py`, `tests/test_core_agent.py`

This is a bug fix. In `supervisor_main()`, add:

```python
knowledge_store = None
try:
    from utils.knowledge_store import ChromaKnowledgeStore
    chroma_path = Path(config.CHROMADB_PATH)
    if chroma_path.exists():
        knowledge_store = ChromaKnowledgeStore(
            str(chroma_path), config.RAG_EMBED_MODEL, config.OLLAMA_ENDPOINT
        )
        log.info("knowledge_store_ready", path=str(chroma_path))
    else:
        log.warning("knowledge_store_missing", path=str(chroma_path),
                    hint="Run --mode rag-refresh to populate it")
except Exception as exc:
    log.warning("knowledge_store_init_failed", error=str(exc))
```

Pass `knowledge_store` to `ToolExecutor(...)` constructor. Confirm `_shared_executor` is constructed with it.

The pattern for instantiating `ChromaKnowledgeStore` already exists in `run_rag_refresh()` (lines 414–419 of `main.py`) — mirror it exactly.

**Tests:**
- `test_supervisor_wires_knowledge_store_when_path_exists` — mock `ChromaKnowledgeStore`, patch `CHROMADB_PATH` to a tmp dir that exists; assert `ToolExecutor` receives the store
- `test_supervisor_skips_knowledge_store_when_path_missing` — patch `CHROMADB_PATH` to nonexistent path; assert no exception raised, `knowledge_store` arg is `None`

---

### Item 93 — HACS + HA Docs Metadata Enrichment

**Files:** `utils/hacs_scraper.py`, `utils/ha_docs_scraper.py`, `utils/knowledge_store.py`, `tests/test_utils.py`

**`hacs_scraper.py` — add `version` to chunk metadata:**
In `chunk_changelog()`, extract the version from each section heading (`## 1.2.3` → `"1.2.3"`) using `re.match(r"^(\d+\.\d+[\.\d]*)", section_heading)`. Store as `"version"` in metadata. If no match, store `""`. Also standardize chunk max size from 2000 to 3000 chars to match the other collections.

**`ha_docs_scraper.py` — add `is_installed` to chunk metadata:**
In `embed_cached_integration_docs()`, add `"is_installed": True` to each chunk's metadata dict. Since only installed integrations are fetched (per `discover_installed_integrations()`), this is always accurate. Enables `where: {"is_installed": True}` filter.

**`knowledge_store.py` — remove `community_cases` collection:**
Remove `"community_cases"` from the `COLLECTIONS` tuple. This collection is empty (Phase 19 placeholder) and wastes one of the per-collection query slots. Phase 19 (`repair-episodes.md`) will add it back when the scraper exists.

Note: existing ChromaDB instances will retain the collection on disk — it just won't be queried or pruned. No migration needed.

**`fetch_ha_release_notes()` in `ha_release_notes_scraper.py` — add `release_type` to tag file metadata:**
The bulk scraper (`fetch_ha_release_notes`) currently only writes the file body with no metadata. In `chunk_release_notes()`, derive `release_type` from the version string:
```python
def _release_type(version: str) -> str:
    if re.search(r"b\d+$", version):
        return "beta"
    parts = version.split(".")
    if len(parts) >= 3 and parts[2] != "0":
        return "patch"
    return "ga"
```
Add `"release_type": _release_type(version)` to the metadata dict in `chunk_release_notes`.

**Tests:**
- `test_hacs_chunk_version_extracted` — section `## 1.2.3\n...` → chunk metadata `version="1.2.3"`
- `test_hacs_chunk_no_version` — section without semver heading → `version=""`
- `test_ha_docs_chunk_is_installed` — `embed_cached_integration_docs` result → metadata includes `is_installed=True`
- `test_community_cases_not_in_collections` — assert `"community_cases" not in knowledge_store.COLLECTIONS`
- `test_release_type_classification` — `"2026.8.0"` → `"ga"`, `"2026.7.2"` → `"patch"`, `"2026.8.0b4"` → `"beta"`

---

### Verification Gate

After all phases are complete:

1. **Stub fix (item 87):** `python -c "import asyncio; from ha_update_manager import fetch_release_notes_cached; print(asyncio.run(fetch_release_notes_cached('2026.8.0', '.cache/ha_release_notes'))[:300])"` — prints real changelog text, not a blog URL.

2. **Blog scraper (items 88–89):** Run `python main.py --mode rag-refresh`. Then: `python -c "from utils.knowledge_store import ChromaKnowledgeStore; import config; s = ChromaKnowledgeStore(config.CHROMADB_PATH, config.RAG_EMBED_MODEL, config.OLLAMA_ENDPOINT); print(s.query('backward incompatible battery_level', top_k=3))"` — returns chunks tagged `category="breaking_change"`.

3. **Supervisor knowledge store (item 92):** Start `python main.py`. In the Chat tab, ask "What are the recent HA breaking changes?" — response cites specific HA content, not "Knowledge store not configured."

4. **Environment profile (items 90–91):** In the Chat tab, ask "What integrations do I have installed?" — response lists actual integration domains from the live HA instance.

5. **Filtered queries (item 89):** In the Chat tab, ask "Are there any breaking changes for the Matter integration in the latest release?" — agent calls `query_knowledge` with `integration_filter=["matter"]`; response is scoped to matter-specific chunks.

6. **Full CI gate:** `black --check . && flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics && mypy --ignore-missing-imports . && bandit -r . -x ./tests,./.venv && pytest --cov --cov-fail-under=90 --ignore=tests/integration`

---

### New Config Keys

| Key | Default | Where |
|---|---|---|
| `HA_PROFILE_REFRESH_HOURS` | `24` | `config.py`, `config.yaml.default`, `setup.sh` |

---

### DB Migrations

| Version | Change |
|---|---|
| v14 | `ha_environment_profile (id INTEGER PRIMARY KEY, profile_json TEXT NOT NULL, last_updated REAL NOT NULL)` |
