"""HA concept documentation scraper and embedder.

Fetches a curated set of Home Assistant concept pages from the home-assistant.io
GitHub repo (entity registry, Lovelace dashboards, automation, scripts, devices,
areas) and embeds them into the ha_concepts ChromaDB collection.

Network calls (fetch_concept_docs) only run during rag-refresh — zero WAN
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

_HA_DOCS_RAW_BASE = (
    "https://raw.githubusercontent.com/home-assistant/home-assistant.io"
    "/current/source"
)

# Curated concept pages: (doc_id, path_under_source)
# path_under_source is appended to _HA_DOCS_RAW_BASE with .markdown extension.
_CONCEPT_DOCS: list[tuple[str, str]] = [
    ("lovelace_dashboards", "lovelace/dashboards"),
    ("lovelace_views", "lovelace/views"),
    ("lovelace_entities_card", "lovelace/entities"),
    ("lovelace_entity_card", "lovelace/entity"),
    ("lovelace_glance_card", "lovelace/glance"),
    ("entity_registry", "_docs/entity_registry"),
    ("area_registry", "_docs/area_registry"),
    ("device_registry", "_docs/device_registry"),
    ("automation_basics", "_docs/automation/index"),
    ("automation_trigger", "_docs/automation/trigger"),
    ("automation_condition", "_docs/automation/condition"),
    ("automation_action", "_docs/automation/action"),
    ("scripts", "_docs/script"),
    ("configuration_yaml", "_docs/configuration"),
    ("integrations_overview", "_docs/configuration/integrations"),
]


def parse_concept_doc(doc_text: str) -> list[str]:
    """Strip Jekyll front matter and split into Markdown sections, chunked to 3000 chars."""
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


def fetch_concept_docs(cache_dir: str) -> int:  # pragma: no cover
    """Fetch curated HA concept docs from GitHub and cache locally.

    Returns count of files newly fetched (cached files are skipped, 404s silently
    ignored).
    """
    import urllib.request

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    fetched = 0
    for doc_id, path in _CONCEPT_DOCS:
        cache_path = Path(cache_dir) / f"{doc_id}.md"
        if cache_path.exists():
            continue
        url = f"{_HA_DOCS_RAW_BASE}/{path}.markdown"
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
        except Exception:  # nosec B110 — 404s and timeouts are expected
            pass
    return fetched


def embed_cached_concept_docs(
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    collected_ids: set[str] | None = None,
) -> int:
    """Read cached concept doc .md files and embed into ha_concepts collection.

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
        chunks = parse_concept_doc(content)
        if not chunks:
            continue
        ids = [f"ha-concepts-{doc_id}-{i}" for i in range(len(chunks))]
        metadatas = [
            {"source": f"ha_concepts/{doc_id}", "doc_id": doc_id} for _ in chunks
        ]
        if collected_ids is not None:
            collected_ids.update(ids)
        knowledge_store.upsert(
            "ha_concepts",
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
        )
        processed += 1
    return processed
