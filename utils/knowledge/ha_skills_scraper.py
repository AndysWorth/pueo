"""HA best-practices skills scraper and embedder.

Fetches SKILL.md and reference files from the homeassistant-ai/skills
repository and embeds them into the ha_best_practices ChromaDB collection.

Content includes a version-stamped table of deprecated HA APIs (e.g. removed
color_temp, removed entered_home/left_home triggers) that prevents Pueo from
proposing fixes using removed APIs.

Network calls (fetch_ha_skills) only run during rag-refresh — zero WAN during
fix cycles.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_GITHUB_BASE = (
    "https://raw.githubusercontent.com/homeassistant-ai/skills/main"
    "/skills/home-assistant-best-practices"
)

# SKILL.md is the top-level doc; the rest are referenced spec files.
# Upstream (homeassistant-ai/skills) currently ships 16 reference files;
# appdaemon.md and examples.yaml are omitted as not relevant to Pueo.
_SKILL_FILES: list[tuple[str, str]] = [
    ("SKILL", "SKILL.md"),
    ("safe-refactoring", "references/safe-refactoring.md"),
    ("triggers-and-conditions", "references/triggers-and-conditions.md"),
    ("automation-actions", "references/automation-actions.md"),
    ("helper-selection", "references/helper-selection.md"),
    ("backups", "references/backups.md"),
    ("blueprint-guide", "references/blueprint-guide.md"),
    ("device-control", "references/device-control.md"),
    ("scenes", "references/scenes.md"),
    ("template-guidelines", "references/template-guidelines.md"),
    ("yaml-only-integrations", "references/yaml-only-integrations.md"),
    ("dashboard-guide", "references/dashboard-guide.md"),
    ("dashboard-cards", "references/dashboard-cards.md"),
    ("domain-docs", "references/domain-docs.md"),
]


def _cache_is_stale(cache_path: Path, max_age_seconds: float) -> bool:
    """Return True when the file is absent or older than max_age_seconds."""
    if not cache_path.exists():
        return True
    return (time.time() - cache_path.stat().st_mtime) >= max_age_seconds


_HEADING = re.compile(r"\n## ")


def _chunk_markdown(
    text: str,
    source: str,
    doc_type: str,
) -> tuple[list[str], list[str], list[dict]]:
    """Split on H2 headings, cap chunks at 3000 chars.

    Returns (ids, documents, metadatas).
    """
    parts = _HEADING.split(text)
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    source_slug = source.replace("/", "-").replace(".", "-").lower()
    for i, part in enumerate(parts):
        chunk = part.strip()
        if not chunk:
            continue
        if len(chunk) > 3000:
            truncated = chunk[:3000]
            space_idx = truncated.rfind(" ")
            chunk = truncated[:space_idx] if space_idx > 0 else truncated
        ids.append(f"ha-skills-{source_slug}-{i}")
        docs.append(chunk)
        metas.append({"source": source, "doc_type": doc_type})
    return ids, docs, metas


def fetch_ha_skills(  # pragma: no cover
    cache_dir: str,
    max_age_hours: int = 168,
) -> int:
    """Fetch HA skills Markdown files from GitHub and cache locally.

    Files are re-fetched when they are absent or older than max_age_hours
    (default: 168 h = 7 days, matching RAG_REFRESH_INTERVAL_HOURS).
    Returns count of files newly fetched or refreshed.
    """
    import urllib.request

    from utils.core.logging import get_logger

    log = get_logger("ha_skills_scraper")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    max_age_secs = max_age_hours * 3600
    fetched = 0
    for slug, filename in _SKILL_FILES:
        cache_path = Path(cache_dir) / f"{slug}.md"
        if not _cache_is_stale(cache_path, max_age_secs):
            log.debug("ha_skills_cached", slug=slug)
            continue
        url = f"{_GITHUB_BASE}/{filename}"
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
                    log.info("ha_skills_fetched", slug=slug)
                else:
                    log.warning(
                        "ha_skills_fetch_failed",
                        slug=slug,
                        url=url,
                        status=resp.status,
                    )
        except Exception as exc:  # nosec B110 — 404s and timeouts are expected
            log.warning("ha_skills_fetch_error", slug=slug, url=url, error=str(exc))
    log.info("ha_skills_fetch_complete", total=len(_SKILL_FILES), fetched=fetched)
    return fetched


_REMOVED_RE = re.compile(r"removed\s+in\s+(20\d\d\.\d+)", re.IGNORECASE)

# Match bare YAML keys inside backticks: `color_temp`, `entered_home`, `battery.low`
_KEY_RE = re.compile(r"`([a-zA-Z_][a-zA-Z0-9_./]*)`")


def _extract_removed_version(why_text: str) -> str | None:
    """Return e.g. '2026.3' from 'The color_temp parameter was removed in 2026.3'."""
    m = _REMOVED_RE.search(why_text)
    return m.group(1) if m else None


def parse_deprecated_keys(cache_dir: str) -> list[dict]:
    """Parse the Critical Anti-Patterns table in SKILL.md for removed YAML keys.

    Returns a list of {key, reason, removed_version} dicts for entries whose
    'Why' column contains 'removed in <version>'.  Writes the result to
    <cache_dir>/deprecated_keys.json so validate_proposed_fix() can load it.
    Returns the list (possibly empty if SKILL.md is absent or has no removals).
    """
    skill_path = Path(cache_dir) / "SKILL.md"
    if not skill_path.exists():
        return []

    text = skill_path.read_text(encoding="utf-8")
    # Find the anti-patterns table: rows starting with '| ' after the heading
    results: list[dict] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if "## Critical Anti-Patterns" in stripped:
            in_table = True
            continue
        if in_table and stripped.startswith("##"):
            break
        if not in_table or not stripped.startswith("|"):
            continue
        cols = [c.strip() for c in stripped.split("|")]
        # cols[0] is empty (before first |), cols[1]=anti-pattern, cols[2]=use instead,
        # cols[3]=why, cols[4]=reference
        if len(cols) < 4:
            continue
        anti_pattern_cell = cols[1]
        why_cell = cols[3] if len(cols) > 3 else ""
        removed_version = _extract_removed_version(why_cell)
        if not removed_version:
            continue
        keys = _KEY_RE.findall(anti_pattern_cell)
        # Also capture `enabled: false` as the key `enabled`
        for raw_key in keys:
            key = raw_key.split(":")[0].strip()  # strip inline values
            if key and key not in ("—",):
                results.append(
                    {
                        "key": key,
                        "reason": why_cell[:200],
                        "removed_version": removed_version,
                    }
                )

    out_path = Path(cache_dir) / "deprecated_keys.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def embed_cached_ha_skills(
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    collected_ids: set[str] | None = None,
) -> int:
    """Read cached ha-skills .md files and embed into ha_best_practices collection.

    If collected_ids is provided, all upserted chunk IDs are added to it.
    Returns count of files embedded.
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
        doc_type = "best_practices" if slug == "SKILL" else "best_practices_ref"
        ids, docs, metas = _chunk_markdown(content, f"ha_skills/{slug}", doc_type)
        if not ids:
            continue
        if collected_ids is not None:
            collected_ids.update(ids)
        knowledge_store.upsert(
            "ha_best_practices",
            ids=ids,
            documents=docs,
            metadatas=metas,
        )
        processed += 1
    return processed
