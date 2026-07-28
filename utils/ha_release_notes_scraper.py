"""HA release notes scraper and embedder (item 50).

Reads cached release note files from HA_UPDATE_RELEASE_NOTES_CACHE_DIR,
extracts breaking-changes sections, and upserts them to the knowledge store.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_BREAKING_KEYWORDS = re.compile(r"break|deprecat|remov|rename", re.IGNORECASE)
_SECTION_SPLIT = re.compile(r"\n#+\s+")


def parse_breaking_changes(release_notes: str) -> list[str]:
    """Extract sections containing breaking-change keywords from release note markdown."""
    sections = _SECTION_SPLIT.split(release_notes)
    chunks = [s.strip() for s in sections if _BREAKING_KEYWORDS.search(s)]
    return [c[:2000] for c in chunks] if chunks else [release_notes[:2000]]


def chunk_release_notes(
    release_notes: str,
    version: str,
) -> tuple[list[str], list[str], list[dict]]:
    """Parse release notes into (ids, documents, metadatas) for a ChromaDB upsert."""
    chunks = parse_breaking_changes(release_notes)
    ids = [f"ha-{version}-{i}" for i in range(len(chunks))]
    metadatas = [
        {"source": f"ha_release_notes/{version}", "version": version} for _ in chunks
    ]
    return ids, chunks, metadatas


def scrape_cached_release_notes(
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
) -> int:
    """Read all cached HA release note .txt files and embed them.

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
        ids, docs, metas = chunk_release_notes(content, version)
        if ids:
            knowledge_store.upsert(
                "ha_release_notes", ids=ids, documents=docs, metadatas=metas
            )
            processed += 1
    return processed
