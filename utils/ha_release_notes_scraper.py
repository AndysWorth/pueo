"""HA release notes scraper and embedder (item 50).

Reads cached release note files, extracts all Markdown sections, and upserts
them to the knowledge store.  Active fetching from GitHub is done by
fetch_ha_release_notes() before scrape_cached_release_notes() is called.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_BREAKING_KEYWORDS = re.compile(r"break|deprecat|remov|rename", re.IGNORECASE)
_SECTION_SPLIT = re.compile(r"\n#+\s+")

_GITHUB_RELEASES_URL = "https://api.github.com/repos/home-assistant/core/releases"


def fetch_ha_release_notes(
    cache_dir: str, n_versions: int = 12
) -> int:  # pragma: no cover
    """Fetch the last n_versions HA releases from GitHub and cache as .txt files.

    Skips versions that are already cached (idempotent).  Returns the number
    of newly downloaded files.
    """
    import json
    import urllib.request

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    url = f"{_GITHUB_RELEASES_URL}?per_page={n_versions}"
    req = urllib.request.Request(url, headers={"User-Agent": "pueo-rag-refresh/1.0"})
    try:
        with urllib.request.urlopen(
            req, timeout=20
        ) as resp:  # nosec B310 — hardcoded GitHub API URL
            releases = json.loads(resp.read())
    except Exception:
        return 0

    fetched = 0
    for release in releases:
        tag = release.get("tag_name", "")
        body = release.get("body", "") or ""
        if not tag or not body:
            continue
        cache_path = Path(cache_dir) / f"{tag}.txt"
        if cache_path.exists():
            continue
        try:
            cache_path.write_text(body, encoding="utf-8")
            fetched += 1
        except OSError:
            continue
    return fetched


def parse_release_sections(release_notes: str) -> list[str]:
    """Extract all Markdown sections from release notes, chunked to 3000 chars.

    Every section is embedded so the agent can retrieve features, bug fixes,
    new integrations, and breaking changes — not just the subset matching
    breaking-change keywords.
    """
    sections = _SECTION_SPLIT.split(release_notes)
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
    return result if result else [release_notes[:3000]]


def parse_breaking_changes(release_notes: str) -> list[str]:
    """Extract only sections containing breaking-change keywords.

    Kept for backward compatibility; chunk_release_notes() now uses
    parse_release_sections() to embed all sections.
    """
    sections = _SECTION_SPLIT.split(release_notes)
    chunks = [s.strip() for s in sections if _BREAKING_KEYWORDS.search(s)]
    return [c[:2000] for c in chunks] if chunks else [release_notes[:2000]]


def chunk_release_notes(
    release_notes: str,
    version: str,
    collected_ids: set[str] | None = None,
) -> tuple[list[str], list[str], list[dict]]:
    """Parse release notes into (ids, documents, metadatas) for a ChromaDB upsert.

    If collected_ids is provided, all generated IDs are added to it — useful
    for the caller to track which IDs belong to this refresh run for pruning.
    """
    chunks = parse_release_sections(release_notes)
    ids = [f"ha-{version}-{i}" for i in range(len(chunks))]
    metadatas = [
        {"source": f"ha_release_notes/{version}", "version": version} for _ in chunks
    ]
    if collected_ids is not None:
        collected_ids.update(ids)
    return ids, chunks, metadatas


def scrape_cached_release_notes(
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    collected_ids: set[str] | None = None,
) -> int:
    """Read all cached HA release note .txt files and embed them.

    If collected_ids is provided, all upserted chunk IDs are added to it.
    Returns the number of files processed.
    """
    path = Path(cache_dir)
    if not path.exists():
        return 0
    processed = 0
    for fp in sorted(path.glob("*.txt")):
        version = fp.stem
        try:
            content = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        ids, docs, metas = chunk_release_notes(content, version, collected_ids)
        if ids:
            knowledge_store.upsert(
                "ha_release_notes", ids=ids, documents=docs, metadatas=metas
            )
            processed += 1
    return processed
