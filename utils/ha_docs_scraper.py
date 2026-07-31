"""HA integration documentation scraper and embedder.

Discovers which integrations are active on the HA instance via REST, fetches
their Markdown documentation from the home-assistant.io GitHub repo, and
embeds the content into the ha_integration_docs ChromaDB collection.

Network calls (discover_installed_integrations, fetch_integration_doc) only
run during rag-refresh — zero WAN during fix cycles.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)
_HEADING = re.compile(r"\n#+\s+")
_HA_DOCS_RAW_URL = (
    "https://raw.githubusercontent.com/home-assistant/home-assistant.io"
    "/current/source/integrations/{domain}.markdown"
)

# Domains that are HA internals or abstract base platforms — not worth fetching docs for
_TRIVIAL_DOMAINS = frozenset(
    {
        "automation",
        "binary_sensor",
        "button",
        "calendar",
        "camera",
        "climate",
        "cover",
        "device_tracker",
        "event",
        "fan",
        "group",
        "homeassistant",
        "input_boolean",
        "input_button",
        "input_datetime",
        "input_number",
        "input_select",
        "input_text",
        "light",
        "lock",
        "media_player",
        "number",
        "persistent_notification",
        "person",
        "scene",
        "script",
        "select",
        "sensor",
        "siren",
        "sun",
        "switch",
        "tag",
        "timer",
        "todo",
        "update",
        "vacuum",
        "zone",
    }
)


def discover_installed_integrations(  # pragma: no cover
    ha_url: str,
    ha_token: str,
) -> list[str]:
    """Query HA REST /api/states to get a sorted list of active integration domains.

    Filters out trivial built-in domains that don't have meaningful docs pages.
    Falls back to [] if HA is unreachable or token is missing.
    """
    import json
    import urllib.request

    if not ha_token:
        return []
    try:
        req = urllib.request.Request(
            f"{ha_url}/api/states",
            headers={"Authorization": f"Bearer {ha_token}"},
        )
        with urllib.request.urlopen(
            req, timeout=10
        ) as resp:  # nosec B310 — HA URL from user config
            states = json.loads(resp.read())
    except Exception:
        return []

    domains: set[str] = set()
    for state in states:
        entity_id: str = state.get("entity_id", "")
        if "." in entity_id:
            domain = entity_id.split(".", 1)[0]
            if domain not in _TRIVIAL_DOMAINS:
                domains.add(domain)
    return sorted(domains)


def parse_integration_doc(doc_text: str) -> list[str]:
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


def fetch_integration_doc(  # pragma: no cover
    domain: str,
    cache_dir: str,
) -> bool:
    """Fetch the HA integration doc from GitHub and cache locally.

    Returns True if newly fetched, False if already cached or not found (404).
    """
    import urllib.request

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    cache_path = Path(cache_dir) / f"{domain}.md"
    if cache_path.exists():
        return False
    url = _HA_DOCS_RAW_URL.format(domain=domain)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "pueo-rag-refresh/1.0"}
        )
        with urllib.request.urlopen(
            req, timeout=15
        ) as resp:  # nosec B310 — hardcoded GitHub raw URL
            if resp.status == 200:
                cache_path.write_bytes(resp.read())
                return True
    except Exception:  # nosec B110 — fetch failures are expected (404 = no docs page)
        pass
    return False


def embed_cached_integration_docs(
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    collected_ids: set[str] | None = None,
) -> int:
    """Read cached integration doc .md files and embed into ha_integration_docs.

    If collected_ids is provided, all upserted chunk IDs are added to it.
    Returns count of files embedded.
    """
    path = Path(cache_dir)
    if not path.exists():
        return 0
    processed = 0
    for fp in sorted(path.glob("*.md")):
        domain = fp.stem
        try:
            content = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        chunks = parse_integration_doc(content)
        if not chunks:
            continue
        ids = [f"ha-docs-{domain}-{i}" for i in range(len(chunks))]
        metadatas = [{"source": f"ha_docs/{domain}", "domain": domain} for _ in chunks]
        if collected_ids is not None:
            collected_ids.update(ids)
        knowledge_store.upsert(
            "ha_integration_docs",
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
        )
        processed += 1
    return processed
