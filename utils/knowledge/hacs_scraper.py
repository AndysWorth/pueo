"""HACS integration changelog scraper and embedder (item 51).

Reads cached HACS changelog .md files, splits them by version header,
and upserts them to the knowledge store.  Active fetching from GitHub and
integration discovery from the HA REST API are handled by the functions
prefixed fetch_* and discover_*.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_SECTION_SPLIT = re.compile(r"\n##\s+")
_GITHUB_REPO_RE = re.compile(
    r"https://github\.com/([^/]+/[^/]+)/releases?", re.IGNORECASE
)
_VERSION_HEADING_RE = re.compile(r"^(\d+\.\d+[\.\d]*)")


def _repo_from_release_url(release_url: str) -> str | None:
    """Extract 'org/repo' from a GitHub release URL."""
    m = _GITHUB_REPO_RE.match(release_url)
    return m.group(1) if m else None


def parse_changelog(changelog_text: str) -> list[str]:
    """Split a HACS changelog into per-version sections."""
    sections = _SECTION_SPLIT.split(changelog_text)
    return [s.strip()[:3000] for s in sections if s.strip()]


def _version_from_section(section_text: str) -> str:
    """Extract semver from the first line of a changelog section, or ''."""
    first_line = section_text.split("\n", 1)[0].lstrip("#").strip()
    m = _VERSION_HEADING_RE.match(first_line)
    return m.group(1) if m else ""


def chunk_changelog(
    changelog_text: str,
    slug: str,
    collected_ids: set[str] | None = None,
) -> tuple[list[str], list[str], list[dict]]:
    """Parse changelog into (ids, documents, metadatas) for a ChromaDB upsert.

    If collected_ids is provided, all generated IDs are added to it.
    """
    chunks = parse_changelog(changelog_text)
    if not chunks:
        return [], [], []
    ids = [f"hacs-{slug}-{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "source": f"hacs/{slug}",
            "slug": slug,
            "version": _version_from_section(chunk),
        }
        for chunk in chunks
    ]
    if collected_ids is not None:
        collected_ids.update(ids)
    return ids, chunks, metadatas


def _discover_via_hacs_api(  # pragma: no cover
    ha_url: str,
    ha_token: str,
) -> list[tuple[str, str]]:
    """Query the HACS REST API for installed integrations.

    GET /api/hacs/repositories returns a list of tracked repositories.  Each
    entry includes 'slug', 'installed', 'category', and 'full_name' (org/repo).
    Returns [] on any error so the caller can fall back to the entity-scan path.
    """
    import json
    import urllib.request

    try:
        req = urllib.request.Request(
            f"{ha_url}/api/hacs/repositories",
            headers={"Authorization": f"Bearer {ha_token}"},
        )
        with urllib.request.urlopen(
            req, timeout=10
        ) as resp:  # nosec B310 — HA URL from user config
            repos = json.loads(resp.read())
    except Exception:
        return []

    pairs: list[tuple[str, str]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        if not repo.get("installed"):
            continue
        if repo.get("category") != "integration":
            continue
        slug: str = repo.get("slug", "")
        full_name: str = repo.get("full_name", "")
        if not slug or not full_name or "/" not in full_name:
            continue
        pairs.append((slug, full_name))
    return pairs


def discover_hacs_integrations(  # pragma: no cover
    ha_url: str,
    ha_token: str,
) -> list[tuple[str, str]]:
    """Discover HACS-managed integrations installed on HA.

    Tries the HACS REST API first (/api/hacs/repositories), then falls back to
    scanning update.* entities from /api/states.  The HACS manager itself
    (hacs/integration) is excluded from both paths.

    Returns a list of (slug, github_repo) pairs.  Falls back to [] if HA is
    unreachable or the token is missing.
    """
    import json
    import urllib.request

    if not ha_token:
        return []

    # Primary: HACS REST API — more reliable, includes integrations without GitHub releases
    pairs = _discover_via_hacs_api(ha_url, ha_token)
    if pairs:
        return pairs

    # Fallback: scan update.* entities for HACS-managed components
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

    pairs = []
    for state in states:
        entity_id: str = state.get("entity_id", "")
        attrs: dict = state.get("attributes", {})
        if not entity_id.startswith("update."):
            continue
        # Custom/HACS components use brands.home-assistant.io/_/ (with underscore);
        # HA built-ins use the path without the underscore prefix. platform is always
        # None on modern HA so we can't rely on it.
        entity_picture: str = attrs.get("entity_picture") or ""
        if "brands.home-assistant.io/_/" not in entity_picture:
            continue
        release_url: str = attrs.get("release_url", "") or ""
        repo = _repo_from_release_url(release_url) if release_url else None
        if not repo:
            continue
        if repo == "hacs/integration":
            continue
        slug = repo.split("/")[-1]
        pairs.append((slug, repo))
    return pairs


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
    collected_ids: set[str] | None = None,
) -> int:
    """Read all cached HACS changelog .md files and embed them.

    If collected_ids is provided, all upserted chunk IDs are added to it.
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
        ids, docs, metas = chunk_changelog(content, slug, collected_ids)
        if ids:
            knowledge_store.upsert(
                "hacs_changelogs", ids=ids, documents=docs, metadatas=metas
            )
            processed += 1
    return processed
