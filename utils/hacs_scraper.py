"""HACS integration changelog scraper and embedder (item 51).

Reads cached HACS changelog .md files, splits them by version header,
and upserts them to the knowledge store.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_SECTION_SPLIT = re.compile(r"\n##\s+")


def parse_changelog(changelog_text: str) -> list[str]:
    """Split a HACS changelog into per-version sections."""
    sections = _SECTION_SPLIT.split(changelog_text)
    return [s.strip()[:2000] for s in sections if s.strip()]


def chunk_changelog(
    changelog_text: str,
    slug: str,
) -> tuple[list[str], list[str], list[dict]]:
    """Parse changelog into (ids, documents, metadatas) for a ChromaDB upsert."""
    chunks = parse_changelog(changelog_text)
    if not chunks:
        return [], [], []
    ids = [f"hacs-{slug}-{i}" for i in range(len(chunks))]
    metadatas = [{"source": f"hacs/{slug}", "slug": slug} for _ in chunks]
    return ids, chunks, metadatas


def fetch_hacs_changelog(  # pragma: no cover
    slug: str,
    repo: str,
    cache_dir: str,
) -> str | None:
    """Fetch a HACS integration changelog from GitHub and cache it locally.

    Tries main then master branch. Returns None on any error.
    """
    import httpx

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    cache_path = Path(cache_dir) / f"{slug}.md"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    for branch in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/CHANGELOG.md"
        try:
            resp = httpx.get(url, timeout=10, follow_redirects=True)
            if resp.status_code == 200:
                cache_path.write_text(resp.text, encoding="utf-8")
                return resp.text
        except Exception:  # nosec B112 — try next branch on any network error
            continue
    return None


def embed_cached_changelogs(
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
) -> int:
    """Read all cached HACS changelog .md files and embed them.

    Returns the number of files processed.
    """
    path = Path(cache_dir)
    if not path.exists():
        return 0
    processed = 0
    for fp in sorted(path.glob("*.md")):
        slug = fp.stem
        try:
            content = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        ids, docs, metas = chunk_changelog(content, slug)
        if ids:
            knowledge_store.upsert(
                "hacs_changelogs", ids=ids, documents=docs, metadatas=metas
            )
            processed += 1
    return processed
