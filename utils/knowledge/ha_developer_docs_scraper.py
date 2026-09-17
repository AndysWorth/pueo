"""HA developer documentation scraper and embedder.

Fetches a curated set of pages from the Home Assistant developer documentation
(developers.home-assistant.io GitHub repo) and embeds them into the
ha_developer_docs ChromaDB collection.

Covers: architecture overview, entity model, config flows, Supervisor API,
WebSocket API, REST API — the internals an agent needs to reason about HA
component behaviour and diagnose integration failures.

Network calls (fetch_developer_docs) only run during rag-refresh — zero WAN
during fix cycles.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)
_HEADING = re.compile(r"\n#+\s+")

_HA_DEV_DOCS_RAW_BASE = (
    "https://raw.githubusercontent.com/home-assistant"
    "/developers.home-assistant/master/docs"
)

# Curated developer doc pages: (doc_id, path_under_docs, category)
# path_under_docs is appended to _HA_DEV_DOCS_RAW_BASE with .md extension.
# Repo was renamed from developers.home-assistant.io → developers.home-assistant;
# several paths were also reorganised (flat root files, api/ subdir, etc.).
_DEVELOPER_DOCS: list[tuple[str, str, str]] = [
    ("architecture_index", "architecture_index", "architecture"),
    ("architecture_components", "architecture_components", "architecture"),
    ("core_entity", "core/entity", "entity"),
    ("core_entity_component", "core/integration/config_flow", "entity"),
    ("config_entries_index", "config_entries_index", "config_flow"),
    ("config_entries_options_flow", "core/integration/options_flow", "config_flow"),
    ("supervisor_index", "supervisor", "supervisor"),
    ("supervisor_developing", "supervisor/development", "supervisor"),
    ("api_websocket", "api/websocket", "api"),
    ("api_rest", "api/rest", "api"),
]


def parse_developer_doc(doc_text: str) -> list[str]:
    """Strip front matter and split into sections, chunked to 3000 chars."""
    text = _FRONTMATTER.sub("", doc_text).strip()
    sections = _HEADING.split(text)
    result = []
    for s in sections:
        s = s.strip()
        if not s:
            continue
        if len(s) <= 3000:
            result.append(s)
        else:
            truncated = s[:3000]
            space_idx = truncated.rfind(" ")
            result.append(truncated[:space_idx] if space_idx > 0 else truncated)
    return result


def fetch_developer_docs(cache_dir: str) -> int:  # pragma: no cover
    """Fetch curated HA developer docs from GitHub and cache locally.

    Returns count of files newly fetched (cached files are skipped).
    Logs a warning for each page that returns a non-200 status or errors.
    """
    import urllib.request
    from utils.core.logging import get_logger

    log = get_logger("ha_developer_docs_scraper")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    fetched = 0
    for doc_id, path, _category in _DEVELOPER_DOCS:
        cache_path = Path(cache_dir) / f"{doc_id}.md"
        if cache_path.exists():
            log.debug("ha_developer_doc_cached", doc_id=doc_id)
            continue
        url = f"{_HA_DEV_DOCS_RAW_BASE}/{path}.md"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "pueo-rag-refresh/1.0"}
            )
            with urllib.request.urlopen(  # nosec B310 — hardcoded GitHub raw URL
                req, timeout=15
            ) as resp:
                if resp.status == 200:
                    cache_path.write_bytes(resp.read())
                    fetched += 1
                    log.info("ha_developer_doc_fetched", doc_id=doc_id, path=path)
                else:
                    log.warning(
                        "ha_developer_doc_fetch_failed",
                        doc_id=doc_id,
                        url=url,
                        status=resp.status,
                    )
        except Exception as exc:  # nosec B110 — 404s and timeouts are expected
            log.warning(
                "ha_developer_doc_fetch_error",
                doc_id=doc_id,
                url=url,
                error=str(exc),
            )
    log.info(
        "ha_developer_docs_fetch_complete",
        total=len(_DEVELOPER_DOCS),
        fetched=fetched,
    )
    return fetched


# Map doc_id to category for metadata enrichment when embedding cached files.
_DOC_CATEGORY: dict[str, str] = {doc_id: cat for doc_id, _path, cat in _DEVELOPER_DOCS}


def embed_cached_developer_docs(
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    collected_ids: set[str] | None = None,
    scraped_for_ha_version: str = "",
) -> int:
    """Read cached developer doc .md files and embed into ha_developer_docs collection.

    If collected_ids is provided, all upserted chunk IDs are added to it.
    Returns count of files embedded.
    """
    path = Path(cache_dir)
    if not path.exists():
        return 0
    processed = 0
    for fp in sorted(path.glob("*.md")):
        doc_id = fp.stem
        try:
            content = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        chunks = parse_developer_doc(content)
        if not chunks:
            continue
        ids = [f"ha-developer-docs-{doc_id}-{i}" for i in range(len(chunks))]
        category = _DOC_CATEGORY.get(doc_id, "general")
        base_meta: dict = {
            "source": f"ha_developer_docs/{doc_id}",
            "doc_id": doc_id,
            "category": category,
        }
        if scraped_for_ha_version:
            base_meta["scraped_for_ha_version"] = scraped_for_ha_version
        metadatas = [dict(base_meta) for _ in chunks]
        if collected_ids is not None:
            collected_ids.update(ids)
        knowledge_store.upsert(
            "ha_developer_docs",
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
        )
        processed += 1
    return processed
