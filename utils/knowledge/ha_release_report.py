"""Deterministic Core release report builder.

Fetches the HA blog post and changelog page, parses BCs and per-integration
changes, and persists a structured JSON report.  No LLM calls.
Called as step 1 of the Core update analysis pipeline before the LLM loop.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel

from utils.core.logging import get_logger

log = get_logger("ha_release_report")

_CHANGELOG_BASE = "https://www.home-assistant.io/changelogs/core-{yymm}"
# GitHub author/PR credit like "(by @user - #123)" at end of changelog lines
_ATTR_REF_RE = re.compile(r"\s*\([^)]*@[^)]*#\d+[^)]*\)\s*$")


class BCEntry(BaseModel):
    name: str
    domains: list[str] = []
    body: str = ""


class ScreeningFlag(BaseModel):
    domain: str
    change: str
    concern: str
    severity: str = "medium"


class ScreeningHighlight(BaseModel):
    domain: str
    feature: str


class CoreReleaseReport(BaseModel):
    version: str
    blog_url: Optional[str] = None
    changelog_url: Optional[str] = None
    bc_installed: list[BCEntry] = []
    bc_general: list[BCEntry] = []
    bc_other_names: list[str] = []
    changes_by_domain: dict[str, list[str]] = {}
    farewell: Optional[str] = None
    # Populated by S3 screening pass
    screening_flags: list[ScreeningFlag] = []
    screening_highlights: list[ScreeningHighlight] = []
    # True when every screening batch was attempted (even if some had flags)
    screening_complete: bool = False
    completeness: str = "incomplete"
    built_at: str = ""


def _derive_changelog_url(version: str) -> str:
    """Derive the changelog URL from a Core version string.

    "2026.11.0" → "https://www.home-assistant.io/changelogs/core-2026.11"
    """
    yymm = version.rsplit(".", 1)[0]
    return _CHANGELOG_BASE.format(yymm=yymm)


def _strip_attr_refs(line: str) -> str:
    """Strip GitHub author/PR refs like '(by @user - #123)' from a line."""
    return _ATTR_REF_RE.sub("", line).rstrip()


def _normalize_name(text: str) -> str:
    """Normalize a display name for fuzzy domain matching."""
    text = re.sub(r"\s*\([^)]*\)", "", text).lower().strip()
    return re.sub(r"[\s\-]+", "_", text)


def _resolve_domain(
    heading: str,
    installed_set: set[str],
    name_to_domain: dict[str, str],
) -> Optional[str]:
    """Map a changelog section heading to an installed domain, or None.

    Priority: (1) exact manifest name lookup, (2) normalized name match.
    """
    candidate = name_to_domain.get(heading)
    if candidate and candidate in installed_set:
        return candidate

    norm = _normalize_name(heading)
    if norm in installed_set:
        return norm
    bare = norm.replace("_", "")
    for dom in installed_set:
        if dom.replace("_", "") == bare:
            return dom
    # Try manifest names with normalisation
    for mname, mdom in name_to_domain.items():
        if mdom in installed_set and _normalize_name(mname) == norm:
            return mdom
    return None


def _matches_name_installed(name: str, installed_set: set[str]) -> bool:
    """Return True when a BC heading (no domain link) matches an installed domain."""
    norm = _normalize_name(name)
    if norm in installed_set:
        return True
    bare = norm.replace("_", "")
    return any(d.replace("_", "") == bare for d in installed_set)


def parse_changelog_sections(
    changelog_text: str,
    installed_set: set[str],
    name_to_domain: dict[str, str],
) -> dict[str, list[str]]:
    """Parse changelog text into per-domain change lists (installed only).

    Expects ``##### Heading`` sections as produced by ``html_to_text``.
    Author/PR credit refs are stripped from each change line.
    """
    result: dict[str, list[str]] = {}
    # Prepend newline so the split pattern \n##### always matches the first section
    parts = re.split(r"\n#{5}\s+", "\n" + changelog_text)
    for part in parts[1:]:  # parts[0] is preamble before first #####
        head_line, _, body = part.partition("\n")
        heading = head_line.strip()
        domain = _resolve_domain(heading, installed_set, name_to_domain)
        if domain is None:
            continue
        lines = []
        for raw in body.splitlines():
            line = raw.strip().lstrip("*-• ").strip()
            if not line:
                continue
            line = _strip_attr_refs(line)
            if line:
                lines.append(line)
        if lines:
            result.setdefault(domain, []).extend(lines)
    return result


async def build_core_release_report(
    version: str,
    installed_integrations: list[str],
    ws_client: Any,  # HAWebSocketClientProtocol
    cache_dir: str,
    state_dir: Path,
    *,
    _github_fetcher: Optional[Callable[[str], str]] = None,
    _blog_fetcher: Optional[Callable[[str], Any]] = None,
    _changelog_fetcher: Optional[Callable[[str], str]] = None,
) -> CoreReleaseReport:
    """Build a structured Core release report.

    Fetches the GitHub release stub, extracts the blog URL, parses BCs, then
    fetches the changelog and maps sections to installed integrations.
    Persists the result to ``state_dir/update_reports/core_<version>.json``.
    """
    import asyncio

    from utils.knowledge.ha_blog_scraper import (
        extract_blog_url_from_stub,
        extract_breaking_changes,
        fetch_blog_post,
        html_to_text,
    )

    installed_set = set(installed_integrations)
    report = CoreReleaseReport(
        version=version,
        built_at=datetime.now(timezone.utc).isoformat(),
    )
    blog_parsed = False
    changelog_parsed = False

    # ── 1. Blog: GitHub stub → blog URL → blog text ───────────────────────
    try:
        if _github_fetcher is not None:
            stub = _github_fetcher(version)
        else:  # pragma: no cover
            stub = await _fetch_core_github_stub(version)

        if len(stub.strip()) < 500:
            report.blog_url = extract_blog_url_from_stub(stub)

        if report.blog_url:
            if _blog_fetcher is not None:
                blog_text = fetch_blog_post(report.blog_url, _fetcher=_blog_fetcher)
            else:  # pragma: no cover
                blog_text = await asyncio.to_thread(fetch_blog_post, report.blog_url)
        else:
            # Non-GA stub: the stub body IS the full notes
            blog_text = stub

        bc_entries = extract_breaking_changes(blog_text)
        for name, domains, body in bc_entries:
            entry = BCEntry(name=name, domains=domains, body=body)
            if not domains:
                if _matches_name_installed(name, installed_set):
                    report.bc_installed.append(entry)
                else:
                    report.bc_general.append(entry)
            elif installed_set.intersection(domains):
                report.bc_installed.append(entry)
            else:
                report.bc_other_names.append(name)

        # Farewell/removed section
        farewell_m = re.search(r"\n##\s+Farewell\b", blog_text, re.IGNORECASE)
        if farewell_m:
            fw_start = farewell_m.start()
            next_fw = re.search(r"\n##\s+", blog_text[fw_start + 4 :])
            fw_end = fw_start + 4 + next_fw.start() if next_fw else len(blog_text)
            report.farewell = blog_text[fw_start:fw_end].strip()

        blog_parsed = True
    except Exception as exc:
        log.warning("release_report_blog_failed", version=version, error=str(exc))

    # ── 2. Manifest map: display name → domain ────────────────────────────
    name_to_domain: dict[str, str] = {}
    try:
        manifests = await ws_client.list_manifests()
        for m in manifests:
            dom = m.get("domain", "")
            name = m.get("name", "")
            if dom and name:
                name_to_domain[name] = dom
    except Exception as exc:
        log.warning("release_report_manifest_failed", version=version, error=str(exc))

    # ── 3. Changelog: fetch, parse, map to installed domains ─────────────
    report.changelog_url = _derive_changelog_url(version)
    try:
        yymm = version.rsplit(".", 1)[0]
        cache_path = Path(cache_dir) / f"changelog_core-{yymm}.txt"

        if cache_path.exists():
            changelog_text = cache_path.read_text(encoding="utf-8")
            changelog_parsed = True
        else:
            if _changelog_fetcher is not None:
                raw = _changelog_fetcher(report.changelog_url)
            else:  # pragma: no cover
                raw = await _fetch_text_url(report.changelog_url)

            if "<" in raw and ">" in raw:
                changelog_text = html_to_text(raw)
            else:
                changelog_text = raw

            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(changelog_text, encoding="utf-8")
            changelog_parsed = True

        report.changes_by_domain = parse_changelog_sections(
            changelog_text, installed_set, name_to_domain
        )
    except Exception as exc:
        log.warning("release_report_changelog_failed", version=version, error=str(exc))

    # ── 4. Completeness + persist ─────────────────────────────────────────
    if blog_parsed and changelog_parsed:
        report.completeness = "complete"

    report_dir = state_dir / "update_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"core_{version}.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )

    return report


def format_core_release_digest(report: "CoreReleaseReport") -> str:
    """Build a compact digest string from a CoreReleaseReport for the main AgentLoop.

    Contains: installed BCs, general BCs, flagged changes, highlight candidates,
    and the Farewell section.  Capped at 15 000 chars.
    """
    parts: list[str] = []

    # Installed BC entries
    if report.bc_installed:
        installed_lines = "\n\n".join(
            f"### {e.name}\n\n{e.body}" if e.body else f"### {e.name}"
            for e in report.bc_installed
        )
        parts.append(
            "## Backward-incompatible changes\n\n"
            "**Affects this instance:**\n\n" + installed_lines
        )
    if report.bc_general:
        general_lines = "\n\n".join(
            f"### {e.name}\n\n{e.body}" if e.body else f"### {e.name}"
            for e in report.bc_general
        )
        general_header = "**General (applies to all installs):**\n\n" + general_lines
        if report.bc_installed:
            parts[-1] += "\n\n" + general_header
        else:
            parts.append("## Backward-incompatible changes\n\n" + general_header)
    if report.bc_other_names:
        other_line = (
            "**Other affected integrations (not installed here):** "
            + ", ".join(report.bc_other_names)
        )
        if parts:
            parts[-1] += "\n\n" + other_line
        else:
            parts.append("## Backward-incompatible changes\n\n" + other_line)

    # Flagged changes from screening pass
    if report.screening_flags:
        flag_lines = []
        for f in report.screening_flags:
            flag_lines.append(
                f"- **{f.domain}** [{f.severity}]: {f.change} — {f.concern}"
            )
        parts.append("## Flagged changes (from screening)\n\n" + "\n".join(flag_lines))

    # Highlight candidates from screening pass
    if report.screening_highlights:
        hl_lines = []
        for h in report.screening_highlights:
            hl_lines.append(f"- **{h.domain}**: {h.feature}")
        parts.append("## Feature highlights (from screening)\n\n" + "\n".join(hl_lines))

    # Farewell / removed section
    if report.farewell:
        parts.append(report.farewell)

    result = "\n\n---\n\n".join(parts)
    return result[:15000]


# Screening batch size budget (chars).  Cut at integration boundaries.
_SCREENING_BATCH_CHARS = 6000


def make_screening_batches(report: "CoreReleaseReport") -> list[str]:
    """Split the report's BC + changelog content into LLM-sized batches.

    Each batch is a self-contained text block ≤ _SCREENING_BATCH_CHARS that
    cuts at integration boundaries (never mid-entry).  Returns a list of
    batch strings.
    """
    sections: list[str] = []

    # BC entries (installed + general) first
    for entry in report.bc_installed + report.bc_general:
        text = (
            f"### BC: {entry.name}\n\n{entry.body}"
            if entry.body
            else f"### BC: {entry.name}"
        )
        sections.append(text)

    # Changelog sections per domain
    for domain, lines in report.changes_by_domain.items():
        text = f"### Changes: {domain}\n\n" + "\n".join(f"- {l}" for l in lines)
        sections.append(text)

    batches: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for section in sections:
        sec_len = len(section)
        if current_parts and current_len + sec_len + 2 > _SCREENING_BATCH_CHARS:
            batches.append("\n\n".join(current_parts))
            current_parts = []
            current_len = 0
        current_parts.append(section)
        current_len += sec_len + 2  # +2 for "\n\n"

    if current_parts:
        batches.append("\n\n".join(current_parts))

    return batches


async def _fetch_core_github_stub(version: str) -> str:  # pragma: no cover
    """Fetch HA Core release notes from the GitHub API."""
    import httpx

    url = f"https://api.github.com/repos/home-assistant/core/releases/tags/{version}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url, headers={"Accept": "application/vnd.github.v3+json"}
        )
        resp.raise_for_status()
        return resp.json().get("body") or ""


async def _fetch_text_url(url: str) -> str:  # pragma: no cover
    """Fetch a URL and return its body as UTF-8 text."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers={"User-Agent": "pueo-rag-refresh/1.0"})
        resp.raise_for_status()
        return resp.text
