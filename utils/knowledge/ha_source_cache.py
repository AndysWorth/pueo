"""Fetch and cache HA component source files from GitHub.

Shared by the rag-refresh pipeline (step 3.2) and fetch_ha_docs tool,
so both callers use the same on-disk cache layout and TTL logic.
"""

from __future__ import annotations

import time
import urllib.request
from pathlib import Path
from typing import Sequence

_HA_SOURCE_RAW_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/dev"
    "/homeassistant/components/{domain}/{filename}"
)
_MAX_HA_DOC_FETCH_CHARS: int = 16_000

# Files fetched for every domain during rag-refresh pre-population.
_PRELOAD_FILES: tuple[str, ...] = ("manifest.json", "__init__.py")


def cache_path_for(cache_dir: str | Path, domain: str, filename: str) -> Path:
    """Return the local cache path for a given domain/filename."""
    return Path(cache_dir) / domain / filename


def is_cache_fresh(path: Path, ttl_seconds: float) -> bool:
    """Return True when the cached file exists and is newer than ttl_seconds."""
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < ttl_seconds


def fetch_and_cache(domain: str, filename: str, cache_dir: str | Path) -> bool:
    """Fetch one HA source file and write it to cache.

    Returns True on success, False on HTTP error or network failure.
    Path-traversal inputs are rejected silently (returns False).
    """
    if "/" in domain or ".." in domain or "/" in filename or ".." in filename:
        return False

    url = _HA_SOURCE_RAW_URL.format(domain=domain, filename=filename)
    req = urllib.request.Request(url, headers={"User-Agent": "pueo-ha-lookup/1.0"})
    try:
        raw: bytes = urllib.request.urlopen(req, timeout=30).read()  # nosec B310
    except Exception:  # nosec B110 — 404s and timeouts are expected for missing domains
        return False

    text = raw.decode("utf-8", errors="replace")[:_MAX_HA_DOC_FETCH_CHARS]
    dest = cache_path_for(cache_dir, domain, filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def preload_domains(
    domains: Sequence[str],
    cache_dir: str | Path,
    ttl_seconds: float,
) -> tuple[int, int]:
    """Fetch manifest.json and __init__.py for each domain if not cached.

    Returns (fetched_count, skipped_count).
    """
    fetched = skipped = 0
    for domain in domains:
        for filename in _PRELOAD_FILES:
            path = cache_path_for(cache_dir, domain, filename)
            if is_cache_fresh(path, ttl_seconds):
                skipped += 1
                continue
            if fetch_and_cache(domain, filename, cache_dir):
                fetched += 1
    return fetched, skipped
