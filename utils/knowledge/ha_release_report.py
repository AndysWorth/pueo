"""Deterministic Core release report builder.

Fetches the HA blog post and changelog page, parses BCs and per-integration
changes, and persists a structured JSON report.  No LLM calls.
Called as step 1 of the Core update analysis pipeline before the LLM loop.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, model_validator

from utils.core.logging import get_logger

log = get_logger("ha_release_report")

_CHANGELOG_BASE = "https://www.home-assistant.io/changelogs/core-{yymm}"
# GitHub author/PR credit like "(by @user - #123)" at end of changelog lines
_ATTR_REF_RE = re.compile(r"\s*\([^)]*@[^)]*#\d+[^)]*\)\s*$")
# HA integration docs URL in a patch body line — extracts the domain slug
_PATCH_HA_DOMAIN_RE = re.compile(
    r"https?://www\.home-assistant\.io/integrations/([^/)#\s\"'>]+)"
)
# Reference-link definition at the bottom of GitHub patch bodies:
#   [mqtt docs]: https://www.home-assistant.io/integrations/mqtt
_PATCH_REF_DEF_RE = re.compile(
    r"^\[([^\]]+)\]:\s+https?://www\.home-assistant\.io/integrations/([^/\s)#\"'>]+)"
)
# Reference-link usage in a fix line: ([mqtt docs]) — no inline URL
_PATCH_REF_USE_RE = re.compile(
    r"\(\[([^\]]+)\]\)(?!\()"
)  # ([label]) not ([label](url))
# Trailing sections to strip from patch note text: ([@user] - [#n]), ([docs](url)), (label)
_PATCH_TRAIL_RE = re.compile(
    r"(?:"
    r"\s*\(\[@[^\]]+\](?:[^)]*[#\d]+[^)]*)?\s*\)"  # ([@user] - [#123])
    r"|\s*\(\[[^\]]+\]\([^)]+\)\)"  # ([text](url))
    r"|\s*\([^)]+\)"  # (anything else)
    r")+$"
)


class ChangeEntry(BaseModel):
    text: str
    prs: list[str] = []


class BCEntry(BaseModel):
    name: str
    domains: list[str] = []
    body: str = ""
    release: str = ""  # "2026.11" for BC from that monthly release; "" = unknown


class ScreeningFlag(BaseModel):
    domain: str
    change: str
    concern: str
    severity: str = "medium"
    # "user" = visible config/automation/entity impact; "developer" = Python API change.
    audience: str = "user"
    # Verbatim Python identifier/constant that custom components might call (developer flags only).
    symbol: Optional[str] = None


class ScreeningHighlight(BaseModel):
    domain: str
    feature: str


class BCEvidence(BaseModel):
    """Entity and configuration evidence for a single BC entry."""

    domains: list[str] = []
    entity_count: int = 0
    entity_examples: list[str] = []
    # Count of matching entities per entity-domain prefix (e.g. {"light": 14, "sensor": 60})
    entities_by_type: dict[str, int] = {}
    # Count of matching entities per integration platform (e.g. {"mobile_app": 2})
    platforms: dict[str, int] = {}
    multi_channel_devices: list[str] = []
    # True when the domains are HA core components that never produce entities (auth, frontend…)
    is_core_component: bool = False
    # Top-level config.yaml keys that name one of this BC's domains (settles YAML-only BCs)
    config_keys_present: list[str] = []
    tokens: list[str] = []
    tokens_searched: int = 0
    tokens_hit: int = 0
    automation_hits: list[str] = []
    dashboard_hits: list[str] = []
    # What the BC text depends on that no evidence here can show (browsers that
    # open the UI, how HA is reached from outside the home).  Stated so "no
    # evidence" is not read as "not affected" (#1213).
    outside_evidence: list[str] = []
    found: bool = False
    # URL classification for auth/http BCs: {"external_url": {"url": "...", "type": "domain", "host": "..."}}
    # Suppresses the "how HA is reached" outside_evidence entry when present.
    url_classification: dict[str, dict[str, str]] = {}
    # Mobile app clients from device registry (for frontend BCs)
    mobile_clients: list[str] = []
    # Sensor entity counts by device_class (for concentration sensor BCs, with zero-fill)
    sensor_device_class_counts: dict[str, int] = {}
    # Per-part key tokens for multi-part BC reason-coverage validation
    bc_part_tokens: list[list[str]] = []


class PueoDependencyHit(BaseModel):
    """One release-note passage that mentions an HA surface Pueo itself uses."""

    # Search term shared by the manifest entries it came from, lowercased ("hacs").
    dependency: str
    # Manifest section of those entries ("integrations" when one of them is there).
    kind: str
    # Where the passage is from: "blog:2026.11", "changelog:2026.11", "patch:2026.11.1".
    source: str
    snippet: str


class CoreReleaseReport(BaseModel):
    version: str
    # installed_version at the time the report was built; "" on old reports.
    # Used to detect stale cached reports when the starting version changes.
    installed_version: str = ""
    blog_url: Optional[str] = None
    # Blog URLs per month covered (YYYY.M → url); populated by multi-month path.
    blog_urls: dict[str, str] = {}
    changelog_url: Optional[str] = None
    bc_installed: list[BCEntry] = []
    bc_general: list[BCEntry] = []
    bc_other_names: list[str] = []
    changes_by_domain: dict[str, list[ChangeEntry]] = {}
    # Patch note changes by domain (from GitHub patch bodies, not the changelog).
    patch_changes_by_domain: dict[str, list[ChangeEntry]] = {}
    # Items that could not be fetched/parsed; e.g. "blog:2026.10", "patch:2026.11.1".
    # Non-empty → completeness="incomplete".
    missing: list[str] = []
    farewell: Optional[str] = None
    # Populated by S3 screening pass
    screening_flags: list[ScreeningFlag] = []
    screening_highlights: list[ScreeningHighlight] = []
    # True when every screening batch finished (even if some had flags)
    screening_complete: bool = False
    # Batch counts from the screening pass; None on reports persisted before #1148.
    screening_batches_total: Optional[int] = None
    screening_batches_done: Optional[int] = None
    # Per-batch results keyed by sha256 of the batch text; enables per-batch reuse.
    # Each value: {"flags": [...], "highlights": [...]}
    screening_batch_results: dict[str, dict] = {}
    # Domains whose batch could not be screened even after split-and-retry.
    screening_unscreened_domains: list[str] = []
    # Mechanical-noise lines dropped during changelog parsing; None on old reports.
    screening_noise_dropped: Optional[int] = None
    # Per-BC entity and config evidence; populated by utils.ha.update_bc_evidence.
    bc_evidence: dict[str, BCEvidence] = {}
    # Per-screening-flag entity and config evidence; keyed by flag change text.
    flag_evidence: dict[str, BCEvidence] = {}
    # Per-BC verdicts from the LLM analysis; persisted by _finish_update_analysis.
    # Each entry: {bc: str, affects: "yes"|"no"|"unknown", reason: str}
    bc_verdicts: list[dict] = []
    # Per-flag verdicts; each entry: {flag: str, affects: "yes"|"no"|"unknown", reason: str}
    flag_verdicts: list[dict] = []
    # Release-note passages that mention an HA surface Pueo depends on (#1233).
    pueo_dependency_hits: list[PueoDependencyHit] = []
    # Per-dependency verdicts from the LLM pueo_impact pass (#1235).
    # Each entry: {dependency: str, verdict: "breaks_pueo"|"no_impact"|"dev_check",
    #              quote: str, reason: str}
    pueo_verdicts: list[dict] = []
    # Deterministic report status (blog + changelog parsed).  Screening status is
    # tracked separately in screening_complete / screening_batches_* (#1148).
    completeness: str = "incomplete"
    built_at: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_changes_by_domain(cls, data: Any) -> Any:
        """Accept old list[str] format from reports persisted before S1."""
        if isinstance(data, dict):
            changes = data.get("changes_by_domain")
            if isinstance(changes, dict):
                coerced: dict[str, list[Any]] = {}
                for domain, entries in changes.items():
                    coerced[domain] = [
                        {"text": e, "prs": []} if isinstance(e, str) else e
                        for e in entries
                    ]
                data = dict(data)
                data["changes_by_domain"] = coerced
        return data


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


# Heading levels in the HA changelog hierarchy
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)")
# PR number references in line text: (#123) or #123
_PR_NUM_RE = re.compile(r"#(\d+)")

# Mechanical-noise patterns — refactor/test/dependency lines that cannot be user-facing
_MECHANICAL_PREFIX_RE = re.compile(
    r"^(?:"
    r"Bump\s"
    r"|Update\s+(?:all\s+)?dependenc"
    r"|Move\b.+\bto\b.+\bmodule\b"
    r"|Define\s+public\s+names"
    r"|Fix\s+flaky\b"
    r"|Add\s+exception\s+translations"
    r"|(?:Add|Fix|Update)\s+tests?\b"
    r")",
    re.IGNORECASE,
)
_MECHANICAL_CONTAINS_RE = re.compile(r"quality[_ ]scale", re.IGNORECASE)


def _is_mechanical_change(text: str) -> bool:
    """Return True for refactor/test/dependency lines that cannot be user-facing."""
    return bool(
        _MECHANICAL_PREFIX_RE.match(text) or _MECHANICAL_CONTAINS_RE.search(text)
    )


def _extract_bc_prs(entries: list[BCEntry]) -> set[str]:
    """Return all PR numbers referenced in BC entry bodies."""
    prs: set[str] = set()
    for entry in entries:
        for m in _PR_NUM_RE.finditer(entry.body):
            prs.add(m.group(1))
    return prs


def parse_changelog_sections(
    changelog_text: str,
    installed_set: set[str],
    name_to_domain: dict[str, str],
    *,
    bc_prs: Optional[set[str]] = None,
) -> tuple[dict[str, list[ChangeEntry]], int]:
    """Parse changelog text into per-domain change lists (installed only).

    Handles multiple heading levels:
    - ``#####`` — integration section (resolved against installed_set)
    - ``#### <single letter>`` — alphabetical separator; ignored (no section change)
    - ``#### Other changes`` — pseudo-domain ``core`` (always included)
    - ``### Frontend`` — pseudo-domain ``frontend`` (always included)
    - Any other heading — ends the current section

    Author/PR credit refs are stripped from each change line.  Lines whose PR
    numbers appear in ``bc_prs`` are dropped (the BC entry is the authority).
    Mechanical-noise lines (bumps, test-only, refactor) are filtered and counted.

    Returns ``(changes_by_domain, noise_dropped_count)``.
    """
    # Pseudo-domains are always "installed"
    effective_installed = installed_set | {"core", "frontend"}
    bc_prs = bc_prs or set()

    result: dict[str, list[ChangeEntry]] = {}
    current_domain: Optional[str] = None
    noise_dropped = 0

    for raw_line in changelog_text.splitlines():
        m = _HEADING_RE.match(raw_line)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()

            if level == 5:
                current_domain = _resolve_domain(
                    heading, effective_installed, name_to_domain
                )
            elif level == 4:
                stripped = heading.strip()
                if len(stripped) == 1 and stripped.upper() == stripped:
                    pass  # alphabetical separator — keep current_domain
                elif stripped.lower() == "other changes":
                    current_domain = "core"
                else:
                    current_domain = None
            elif level == 3 and heading.lower() == "frontend":
                current_domain = "frontend"
            else:
                current_domain = None
            continue

        if current_domain is None:
            continue

        line = raw_line.strip().lstrip("*-• ").strip()
        if not line:
            continue

        prs = _PR_NUM_RE.findall(line)
        line = _strip_attr_refs(line)
        if not line:
            continue

        # Skip lines whose PR number is already in a BC body
        if bc_prs and any(pr in bc_prs for pr in prs):
            continue

        if _is_mechanical_change(line):
            noise_dropped += 1
            continue

        result.setdefault(current_domain, []).append(ChangeEntry(text=line, prs=prs))

    return result, noise_dropped


def release_span(installed: str, target: str) -> tuple[list[str], list[str]]:
    """Return (months, patches) for the upgrade from installed to target.

    months: YYYY.M strings, one per monthly release after the installed month
            up to and including the target month.  Empty for a same-month update.
    patches: patch versions whose notes should be fetched.
             Crossing months → target_month.1 through target (inclusive).
             Same month → installed_patch+1 through target_patch (inclusive).

    Returns ([], []) when either version cannot be parsed.
    """

    def _parse(v: str) -> Optional[tuple[int, int, int]]:
        parts = v.split(".")
        if len(parts) >= 3 and len(parts[0]) == 4 and parts[0].startswith("20"):
            try:
                return (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                pass
        return None

    iv = _parse(installed)
    tv = _parse(target)
    if iv is None or tv is None:
        return [], []

    iy, im, ip = iv
    ty, tm, tp = tv

    months: list[str] = []
    curr_y, curr_m = iy, im + 1
    if curr_m > 12:  # installed in December: start in January of next year
        curr_m = 1
        curr_y += 1
    while (curr_y, curr_m) <= (ty, tm):
        months.append(f"{curr_y}.{curr_m}")
        curr_m += 1
        if curr_m > 12:
            curr_m = 1
            curr_y += 1

    if months:
        patches = [f"{ty}.{tm}.{p}" for p in range(1, tp + 1)]
    else:
        patches = [f"{ty}.{tm}.{p}" for p in range(ip + 1, tp + 1)]

    return months, patches


def parse_patch_notes(
    body: str,
    installed_set: set[str],
    name_to_domain: dict[str, str],
) -> dict[str, list[ChangeEntry]]:
    """Parse a GitHub patch release body into per-domain change lists.

    Handles two link forms used in GitHub patch bodies:
    - Inline URL:    - Fix foo ([@u] - [#1]) ([MQTT docs](https://…/mqtt))
    - Reference:     - Fix foo ([@u] - [#1]) ([mqtt docs])
                     …
                     [mqtt docs]: https://www.home-assistant.io/integrations/mqtt

    Reference definition lines at the bottom are collected in the first pass and
    used to resolve domain slugs in fix lines.  Definition lines are never emitted
    as changes.
    """
    # First pass: collect reference definitions
    ref_to_domain: dict[str, str] = {}
    for raw_line in body.splitlines():
        def_m = _PATCH_REF_DEF_RE.match(raw_line.strip())
        if def_m:
            label = def_m.group(1).lower()
            slug = def_m.group(2).rstrip("/")
            ref_to_domain[label] = slug

    result: dict[str, list[ChangeEntry]] = {}
    for raw_line in body.splitlines():
        # Skip reference definition lines
        if _PATCH_REF_DEF_RE.match(raw_line.strip()):
            continue
        line = raw_line.strip().lstrip("*-• ").strip()
        if not line:
            continue
        # Try inline URL first (e.g. ([Domain docs](https://…/mqtt)))
        domain_m = _PATCH_HA_DOMAIN_RE.search(line)
        if domain_m is not None:
            domain = domain_m.group(1).rstrip("/")
        else:
            # Try reference label (e.g. ([mqtt docs]))
            ref_m = _PATCH_REF_USE_RE.search(line)
            if ref_m is None:
                continue
            label = ref_m.group(1).lower()
            if label not in ref_to_domain:
                continue
            domain = ref_to_domain[label]

        if domain not in installed_set:
            norm = domain.replace("-", "_")
            if norm in installed_set:
                domain = norm
            else:
                continue
        prs = _PR_NUM_RE.findall(line)
        # Strip trailing attribution/docs/label parentheticals to get display text
        text = _PATCH_TRAIL_RE.sub("", line).strip()
        if not text:
            continue
        if _is_mechanical_change(text):
            continue
        result.setdefault(domain, []).append(ChangeEntry(text=text, prs=prs))
    return result


# A release-note line that says something changed, as opposed to merely naming it.
_CHANGE_VERB_RE = re.compile(
    r"\b(?:remov\w*|renam\w*|replac\w*|deprecat\w*|drop(?:s|ped|ping)?"
    r"|no longer|migrat\w*)\b",
    re.IGNORECASE,
)
_MAX_HITS_PER_DEPENDENCY = 6
_HIT_SNIPPET_CHARS = 600


def _term_regex(term: str) -> "re.Pattern[str]":
    """Whole-term, case-insensitive match; ``hacs`` matches ``hacs/x`` but not ``xhacs``."""
    head = r"(?<![\w/])" if term[0].isalnum() else ""
    tail = r"(?!\w)" if term[-1].isalnum() else ""
    return re.compile(head + re.escape(term) + tail, re.IGNORECASE)


def _is_specific_term(term: str, sections: set[str]) -> bool:
    """An integration name, CLI command or full path is worth a hit wherever it appears.

    A generic word or phrase (``repairs``, ``automation trace``) is not: it counts
    only on a line that also says something changed.
    """
    return bool(sections & {"integrations", "ssh_commands"}) or "/" in term


def find_pueo_dependency_hits(
    texts: list[tuple[str, str]],
    manifest: dict[str, list[dict[str, Any]]],
) -> list[PueoDependencyHit]:
    """Find release-note passages that mention something Pueo depends on.

    ``texts`` is ``[(source, text), ...]``; each non-heading line of a text is one
    candidate passage.  Deterministic, no LLM: it gathers evidence only, and a
    later step judges whether a passage means Pueo breaks.  Per dependency the
    passages with a change verb come first, and at most
    ``_MAX_HITS_PER_DEPENDENCY`` are kept.
    """
    from utils.ha.pueo_dependencies import dependency_term_index

    hits: list[PueoDependencyHit] = []
    for term, owners in sorted(dependency_term_index(manifest).items()):
        sections = {section for section, _ in owners}
        kind = "integrations" if "integrations" in sections else owners[0][0]
        specific = _is_specific_term(term, sections)
        rx = _term_regex(term)
        with_verb: list[PueoDependencyHit] = []
        without_verb: list[PueoDependencyHit] = []
        seen: set[str] = set()
        for source, text in texts:
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                m = rx.search(line)
                if m is None:
                    continue
                has_verb = _CHANGE_VERB_RE.search(line) is not None
                if not specific and not has_verb:
                    continue
                snippet = line
                if len(snippet) > _HIT_SNIPPET_CHARS:
                    start = max(0, m.start() - _HIT_SNIPPET_CHARS // 3)
                    snippet = snippet[start : start + _HIT_SNIPPET_CHARS].strip()
                if snippet in seen:
                    continue
                seen.add(snippet)
                hit = PueoDependencyHit(
                    dependency=term, kind=kind, source=source, snippet=snippet
                )
                (with_verb if has_verb else without_verb).append(hit)
        hits.extend((with_verb + without_verb)[:_MAX_HITS_PER_DEPENDENCY])
    return hits


async def build_core_release_report(
    version: str,
    installed_integrations: list[str],
    ws_client: Any,  # HAWebSocketClientProtocol
    cache_dir: str,
    state_dir: Path,
    *,
    installed_version: str = "",
    _github_fetcher: Optional[Callable[[str], str]] = None,
    _blog_fetcher: Optional[Callable[[str], Any]] = None,
    _changelog_fetcher: Optional[Callable[[str], str]] = None,
    _pueo_manifest: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> CoreReleaseReport:
    """Build a structured Core release report.

    Fetches the GitHub release stubs, extracts blog URLs per monthly release,
    parses BCs, patch notes, and the changelog.  Persists the result to
    ``state_dir/update_reports/core_<version>.json``.

    When ``installed_version`` is supplied the report covers every monthly
    release between the installed and target versions (BCs from each blog) plus
    every patch in the target month.  Completeness fails closed: ``"complete"``
    only when every required fetch and parse succeeded.
    """
    import asyncio

    from utils.knowledge.ha_blog_scraper import (
        _BC_SECTION_RE,
        extract_blog_url_from_stub,
        extract_breaking_changes,
        fetch_blog_post,
        html_to_text,
    )

    installed_set = set(installed_integrations)
    months, patches = (
        release_span(installed_version, version) if installed_version else ([], [])
    )

    report = CoreReleaseReport(
        version=version,
        installed_version=installed_version,
        built_at=datetime.now(timezone.utc).isoformat(),
    )
    # Full text of everything fetched, for the Pueo dependency scan (step 5).
    scan_texts: list[tuple[str, str]] = []

    async def _get_stub(ver: str) -> str:
        if _github_fetcher is not None:
            return _github_fetcher(ver)
        return await _fetch_core_github_stub(ver)  # pragma: no cover

    def _add_bc_entries(bc_entries: list, release_label: str) -> None:
        for name, domains, body in bc_entries:
            entry = BCEntry(
                name=name, domains=domains, body=body, release=release_label
            )
            if not domains:
                if _matches_name_installed(name, installed_set):
                    report.bc_installed.append(entry)
                else:
                    report.bc_general.append(entry)
            elif installed_set.intersection(domains):
                report.bc_installed.append(entry)
            else:
                if name not in report.bc_other_names:
                    report.bc_other_names.append(name)

    # ── 1. Blog and BCs ───────────────────────────────────────────────────
    if months:
        # New path: fetch .0 stub for each monthly release in range
        for month in months:
            month_zero = f"{month}.0"
            try:
                stub = await _get_stub(month_zero)
                blog_url = (
                    extract_blog_url_from_stub(stub)
                    if len(stub.strip()) < 500
                    else None
                )
                if not blog_url:
                    report.missing.append(f"blog:{month}")
                    continue
                report.blog_urls[month] = blog_url
                if month == months[-1]:
                    report.blog_url = blog_url

                if _blog_fetcher is not None:
                    blog_text = fetch_blog_post(blog_url, _fetcher=_blog_fetcher)
                else:  # pragma: no cover
                    blog_text = await asyncio.to_thread(fetch_blog_post, blog_url)

                scan_texts.append((f"blog:{month}", blog_text))
                if not _BC_SECTION_RE.search(blog_text):
                    report.missing.append(f"bc_section:{month}")

                _add_bc_entries(extract_breaking_changes(blog_text), month)

                if month == months[-1]:
                    farewell_m = re.search(
                        r"\n##\s+Farewell\b", blog_text, re.IGNORECASE
                    )
                    if farewell_m:
                        fw_start = farewell_m.start()
                        next_fw = re.search(r"\n##\s+", blog_text[fw_start + 4 :])
                        fw_end = (
                            fw_start + 4 + next_fw.start()
                            if next_fw
                            else len(blog_text)
                        )
                        report.farewell = blog_text[fw_start:fw_end].strip()
            except Exception as exc:
                log.warning(
                    "release_report_blog_failed", version=month_zero, error=str(exc)
                )
                if f"blog:{month}" not in report.missing:
                    report.missing.append(f"blog:{month}")
    elif not installed_version:
        # Fallback/backward-compat: fetch target version's stub directly.
        # Only used when installed_version is not known.  When installed_version
        # is set and months=[], this is a same-month patch — no blog to fetch.
        blog_parsed_legacy = False
        try:
            stub = await _get_stub(version)
            if len(stub.strip()) < 500:
                report.blog_url = extract_blog_url_from_stub(stub)
            if report.blog_url:
                if _blog_fetcher is not None:
                    blog_text = fetch_blog_post(report.blog_url, _fetcher=_blog_fetcher)
                else:  # pragma: no cover
                    blog_text = await asyncio.to_thread(
                        fetch_blog_post, report.blog_url
                    )
            else:
                blog_text = stub

            scan_texts.append((f"blog:{version.rsplit('.', 1)[0]}", blog_text))
            _add_bc_entries(extract_breaking_changes(blog_text), "")
            farewell_m = re.search(r"\n##\s+Farewell\b", blog_text, re.IGNORECASE)
            if farewell_m:
                fw_start = farewell_m.start()
                next_fw = re.search(r"\n##\s+", blog_text[fw_start + 4 :])
                fw_end = fw_start + 4 + next_fw.start() if next_fw else len(blog_text)
                report.farewell = blog_text[fw_start:fw_end].strip()
            blog_parsed_legacy = True
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

    # ── 3. Patch notes (from GitHub patch bodies) ─────────────────────────
    for patch in patches:
        try:
            patch_body = await _get_stub(patch)
            scan_texts.append((f"patch:{patch}", patch_body))
            patch_changes = parse_patch_notes(patch_body, installed_set, name_to_domain)
            for domain, entries in patch_changes.items():
                report.patch_changes_by_domain.setdefault(domain, []).extend(entries)
            log.info(
                "patch_notes_parsed",
                patch=patch,
                domains=len(patch_changes),
            )
        except Exception as exc:
            log.warning("release_report_patch_failed", patch=patch, error=str(exc))
            report.missing.append(f"patch:{patch}")

    # ── 4. Changelog (target month or legacy .0 release) ─────────────────
    # Fetch when crossing months (new path) OR when installed_version is unknown
    # (legacy path for .0 upgrades). Same-month patches skip this entirely.
    report.changelog_url = _derive_changelog_url(version)
    changelog_parsed = False
    if months or not installed_version:
        try:
            yymm = version.rsplit(".", 1)[0]
            # Key by full version so a new patch always refetches once.
            # Fall back to the old month-keyed file only when the new one is
            # absent and the fetch itself fails.
            new_cache_path = Path(cache_dir) / f"changelog_core-{version}.txt"
            old_cache_path = Path(cache_dir) / f"changelog_core-{yymm}.txt"

            if new_cache_path.exists():
                changelog_text = new_cache_path.read_text(encoding="utf-8")
            else:
                try:
                    if _changelog_fetcher is not None:
                        raw = _changelog_fetcher(report.changelog_url)
                    else:  # pragma: no cover
                        raw = await _fetch_text_url(report.changelog_url)

                    if "<" in raw and ">" in raw:
                        changelog_text = html_to_text(raw)
                    else:
                        changelog_text = raw

                    new_cache_path.parent.mkdir(parents=True, exist_ok=True)
                    new_cache_path.write_text(changelog_text, encoding="utf-8")
                except Exception:
                    # Fallback to old month-named cache when fetch fails
                    if old_cache_path.exists():
                        changelog_text = old_cache_path.read_text(encoding="utf-8")
                    else:
                        raise

            scan_texts.append((f"changelog:{yymm}", changelog_text))
            bc_prs = _extract_bc_prs(report.bc_installed + report.bc_general)
            changes, noise_dropped = parse_changelog_sections(
                changelog_text, installed_set, name_to_domain, bc_prs=bc_prs
            )
            report.changes_by_domain = changes
            report.screening_noise_dropped = noise_dropped
            changelog_parsed = True
            log.info(
                "changelog_parsed",
                version=version,
                domains=len(changes),
                noise_dropped=noise_dropped,
            )
        except Exception as exc:
            log.warning(
                "release_report_changelog_failed", version=version, error=str(exc)
            )
            report.missing.append(f"changelog:{version.rsplit('.', 1)[0]}")
    else:
        # Same-month patch (installed_version set, months=[]): changelog not needed
        changelog_parsed = True

    # ── 5. Pueo dependency scan ───────────────────────────────────────────
    # Over the full text, not the parsed sections: what breaks Pueo may be in
    # blog prose or under an integration this instance does not have (#1233).
    if _pueo_manifest is None:
        from utils.ha.pueo_dependencies import PUEO_HA_DEPENDENCIES

        _pueo_manifest = PUEO_HA_DEPENDENCIES
    report.pueo_dependency_hits = find_pueo_dependency_hits(scan_texts, _pueo_manifest)
    log.info(
        "pueo_dependency_scan",
        version=version,
        hits=len(report.pueo_dependency_hits),
        dependencies=sorted({h.dependency for h in report.pueo_dependency_hits}),
    )

    # ── 6. Completeness ───────────────────────────────────────────────────
    if months:
        # New path: complete only when nothing is missing
        if not report.missing:
            report.completeness = "complete"
    elif installed_version:
        # Same-month patch (installed_version set, months=[]): complete when
        # all patches fetched and changelog not required
        if not report.missing:
            report.completeness = "complete"
    else:
        # Legacy path (installed_version not known): use old blog+changelog gate
        if blog_parsed_legacy and changelog_parsed:  # type: ignore[possibly-undefined]
            if not report.missing:
                report.completeness = "complete"

    # ── 7. Persist ────────────────────────────────────────────────────────
    report_dir = state_dir / "update_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"core_{version}.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )

    return report


_DIGEST_SOFT_CAP = 12000
_SEP = "\n\n---\n\n"


def _evidence_lines(evidence: "BCEvidence") -> list[str]:
    """Return bullet lines summarising a BCEvidence object.

    States negatives explicitly so the LLM can conclude 'no' without tools:
    - "Multi-channel devices: none" when entities found but none are multi-channel
    - "Top-level `<domain>:` YAML key: absent" when domain present but not in config.yaml
    - "Searched … — 0 hits" when tokens were searched but nothing matched
    """
    ev_lines: list[str] = []
    if evidence.domains or evidence.entity_count > 0:
        if evidence.entity_count > 0:
            ex = ", ".join(evidence.entity_examples[:5])
            suffix = "…" if len(evidence.entity_examples) > 5 else ""
            ev_lines.append(f"Entities ({evidence.entity_count}): {ex}{suffix}")
            if evidence.entities_by_type:
                breakdown = ", ".join(
                    f"{t} {n}" for t, n in sorted(evidence.entities_by_type.items())
                )
                ev_lines.append(f"By type: {breakdown}")
                # Types the BC names but this instance has none of: say so in
                # words, a "light 0" inside a list is easy to miss (#1209).
                zero_types = sorted(
                    t for t, n in evidence.entities_by_type.items() if n == 0
                )
                if zero_types:
                    ev_lines.append(f"No entities of type: {', '.join(zero_types)}")
            if evidence.platforms:
                plat = ", ".join(
                    f"{p} {n}" for p, n in sorted(evidence.platforms.items())
                )
                ev_lines.append(f"Platforms: {plat}")
            if evidence.multi_channel_devices:
                ev_lines.append(
                    f"Multi-channel devices: {', '.join(evidence.multi_channel_devices)}"
                )
            else:
                ev_lines.append("Multi-channel devices: none")
        elif evidence.is_core_component:
            ev_lines.append(
                f"{'/'.join(evidence.domains)}: core component (no entities)"
                " — judge from text and config"
            )
        elif evidence.domains:
            ev_lines.append(f"0 {'/'.join(evidence.domains)} entities on this instance")
    # URL classification: show which URL types are configured (auth/http BCs)
    if evidence.url_classification:
        for key, info in sorted(evidence.url_classification.items()):
            url_type = info.get("type", "")
            host = info.get("host", "")
            label = key.replace("_", " ").title()
            if url_type and host:
                ev_lines.append(f"{label}: {url_type} ({host})")
            elif url_type:
                ev_lines.append(f"{label}: {url_type}")
    # Mobile clients: list mobile_app devices (frontend BCs)
    if evidence.mobile_clients:
        ev_lines.append(f"Mobile app clients: {', '.join(evidence.mobile_clients[:5])}")
    # Sensor device_class counts (concentration sensor BCs)
    if evidence.sensor_device_class_counts:
        counts = ", ".join(
            f"{dc} {n}" for dc, n in sorted(evidence.sensor_device_class_counts.items())
        )
        ev_lines.append(f"Sensor device classes: {counts}")
        zero_dcs = sorted(
            dc for dc, n in evidence.sensor_device_class_counts.items() if n == 0
        )
        if zero_dcs:
            ev_lines.append(f"No sensors of class: {', '.join(zero_dcs)}")
    if evidence.outside_evidence:
        ev_lines.append(
            "Outside the evidence (only the user knows): "
            + "; ".join(evidence.outside_evidence)
        )
    if evidence.config_keys_present:
        ev_lines.append(
            f"Config keys present: {', '.join(evidence.config_keys_present)}"
        )
    # Absent YAML keys: when no entities found, state which domains had no YAML key.
    # Only meaningful for YAML-configured integrations (entity_count==0 avoids noise for
    # UI-configured integrations like tuya/hue/matter that never have a top-level YAML key).
    if (
        evidence.domains
        and not evidence.is_core_component
        and evidence.entity_count == 0
    ):
        absent = [d for d in evidence.domains if d not in evidence.config_keys_present]
        for d in absent[:3]:
            ev_lines.append(f"Top-level `{d}:` YAML key: absent")
    if evidence.automation_hits:
        ev_lines.append(
            f"Config refs ({len(evidence.automation_hits)}): "
            + "; ".join(evidence.automation_hits[:2])
        )
    if evidence.dashboard_hits:
        ev_lines.append(
            f"Dashboard refs ({len(evidence.dashboard_hits)}): "
            + "; ".join(evidence.dashboard_hits[:2])
        )
    if evidence.tokens:
        searched = evidence.tokens_searched or len(evidence.tokens)
        if evidence.tokens_hit and evidence.tokens_hit > 0:
            ev_lines.append(
                f"Tokens checked ({evidence.tokens_hit}/{searched} hit):"
                f" {', '.join(evidence.tokens[:8])}"
            )
        else:
            ev_lines.append(
                f"Searched automations/dashboards for"
                f" {searched} token(s): {', '.join(evidence.tokens[:8])} — 0 hits"
            )
    return ev_lines


def evidence_proof_lines(evidence: "BCEvidence") -> list[str]:
    """Return the evidence lines that can prove a BC applies to this install.

    These are the line kinds `seed_update_bc.md` names as proof for an
    `action_needed` verdict: config refs, dashboard refs, a token search with
    hits, and config keys present.  Entity counts are not proof.  An empty
    result means nothing in the evidence can ground `action_needed` (#1223).
    """
    has_token_hits = bool(evidence.tokens and evidence.tokens_hit > 0)
    prefixes = ["Config refs (", "Dashboard refs (", "Config keys present:"]
    if has_token_hits:
        prefixes.append("Tokens checked (")
    return [
        line
        for line in _evidence_lines(evidence)
        if any(line.startswith(p) for p in prefixes)
    ]


def _format_bc_entry(e: "BCEntry", evidence: Optional["BCEvidence"] = None) -> str:
    """Format a single BC entry as Markdown, appending evidence when available."""
    text = f"### {e.name}\n\n{e.body}" if e.body else f"### {e.name}"
    if evidence is None:
        return text
    ev_lines = _evidence_lines(evidence)
    if ev_lines:
        text += "\n\nEvidence:\n" + "\n".join(f"- {line}" for line in ev_lines)
    return text


def _format_flag_entry(
    f: "ScreeningFlag", evidence: Optional["BCEvidence"] = None
) -> str:
    """Format a screening flag as Markdown, appending evidence when available."""
    text = f"- **{f.domain}** [{f.severity}]: {f.change} — {f.concern}"
    if evidence is None:
        return text
    ev_lines = _evidence_lines(evidence)
    if ev_lines:
        text += "\n  Evidence:\n" + "\n".join(f"  - {line}" for line in ev_lines)
    return text


def _verdict_symbol(v: dict) -> str:
    """Return a short label for a BC verdict dict (new or old format)."""
    verdict = v.get("verdict")
    if verdict == "action_needed":
        when = v.get("when")
        if when == "after_update":
            return "⚡ action after update"
        return "🚫 fix before update"
    if verdict == "no_action":
        return "✅ no action"
    if verdict == "user_check":
        return "❓ check yourself"
    if verdict == "not_assessed":
        return "⬜ not assessed"
    # Backward compat: old affects field
    affects = v.get("affects", "")
    if not affects:
        return "⬜ not assessed"
    return {"yes": "⚠️ yes", "no": "✅ no"}.get(affects, "❓ unknown")


def format_core_release_digest(report: "CoreReleaseReport") -> str:
    """Build a compact digest for the main AgentLoop from a CoreReleaseReport.

    Ordering:
    1. BC verdict table (name | verdict | reason | fix note)
    2. Patch changes
    3. Feature highlights (≤ 8)
    4. Flags — high/medium only, one line each, marked informational
    5. Other-integration names

    No hard char slice.  When the text would exceed _DIGEST_SOFT_CAP, flags are
    dropped first, then highlights, and an explicit "omitted: …" note is appended.
    "digest:flags_omitted" or "digest:highlights_omitted" is added to report.missing
    so completeness is downgraded.
    """
    _SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
    all_bc = list(report.bc_installed) + list(report.bc_general)
    # Build a lookup from report.bc_verdicts (may be pre-populated by assessment pass)
    _verdict_map: dict[str, dict] = {
        v["bc"]: v for v in report.bc_verdicts if isinstance(v, dict) and "bc" in v
    }

    # 1. BC verdict table
    _bc_rows: list[str] = []
    for e in all_bc:
        v = _verdict_map.get(e.name, {})
        reason = v.get("reason", "not yet assessed")
        fix = v.get("config_fix")
        fix_note = f" | fix: {fix.get('description', '')}." if fix else ""
        action_note = ""
        if v.get("verdict") == "action_needed":
            if v.get("action"):
                action_note = f" | action: {v['action']}"
            if v.get("when"):
                action_note += f" ({v['when'].replace('_', ' ')})"
        elif v.get("verdict") == "user_check" and v.get("user_question"):
            action_note = f" | check: {v['user_question']}"
        label = "installed" if e in report.bc_installed else "general"
        _bc_rows.append(
            f"- **{e.name}** [{label}]: {_verdict_symbol(v)} — {reason}{action_note}{fix_note}"
        )
    # Empty-BC explanation
    if not all_bc:
        bc_missing = [
            m
            for m in report.missing
            if m.startswith(("blog:", "bc_section:", "changelog:"))
        ]
        if bc_missing:
            _bc_rows.append(
                "Could not read breaking-change data for: " + ", ".join(bc_missing)
            )
        elif report.installed_version:
            months_span, _ = release_span(report.installed_version, report.version)
            if not months_span:
                yymm = report.version.rsplit(".", 1)[0]
                _bc_rows.append(
                    f"No backward-incompatible changes apply (patch update within {yymm})"
                )
            else:
                _bc_rows.append(
                    "No backward-incompatible changes found for this instance"
                )

    bc_section = ""
    if _bc_rows or all_bc:
        bc_section = "## Backward-incompatible changes\n\n" + "\n".join(_bc_rows)
        if report.bc_other_names:
            bc_section += (
                "\n\n**Other integrations (not installed here):** "
                + ", ".join(report.bc_other_names)
            )

    # 2. Patch changes (installed domains only — parse_patch_notes already filtered)
    patch_text = ""
    if report.patch_changes_by_domain:
        patch_lines: list[str] = []
        for domain, entries in report.patch_changes_by_domain.items():
            entry_lines = "\n".join(f"- {e.text}" for e in entries)
            patch_lines.append(f"**{domain}:**\n{entry_lines}")
        patch_text = "## Patch changes\n\n" + "\n\n".join(patch_lines)

    # 3. Feature highlights (≤ 8)
    hl_text = ""
    if report.screening_highlights:
        hl_lines = [
            f"- **{h.domain}**: {h.feature}" for h in report.screening_highlights[:8]
        ]
        hl_text = "## Feature highlights (from screening)\n\n" + "\n".join(hl_lines)

    # 4. Flags — high/medium only, informational
    flag_text = ""
    _sig_flags = [f for f in report.screening_flags if f.severity in ("high", "medium")]
    if _sig_flags:
        sorted_flags = sorted(_sig_flags, key=lambda f: _SEV_ORDER.get(f.severity, 1))
        flag_lines = [
            f"- **{f.domain}** [{f.severity.upper()}]: {f.change} _(informational)_"
            for f in sorted_flags
        ]
        flag_text = (
            "## Flagged changes (informational only — do not raise impact)\n\n"
            + "\n".join(flag_lines)
        )

    # 5. Other integrations already embedded in bc_section above.

    # Assemble with soft-cap enforcement: drop flags, then highlights, then note it.
    core_parts = [p for p in [bc_section, patch_text] if p]
    core_text = _SEP.join(core_parts)

    def _join(*parts: str) -> str:
        return _SEP.join(p for p in parts if p)

    full = _join(core_text, hl_text, flag_text)
    if len(full) <= _DIGEST_SOFT_CAP:
        return full

    # Over cap: drop flags first
    no_flags = _join(core_text, hl_text)
    if len(no_flags) > _DIGEST_SOFT_CAP:
        # Drop highlights too
        no_hl = _join(core_text)
        omit_note = _join(
            no_hl,
            "_(omitted: highlights, flags — over digest size limit)_",
        )
        if "digest:flags_omitted" not in report.missing:
            report.missing.append("digest:flags_omitted")
        if "digest:highlights_omitted" not in report.missing:
            report.missing.append("digest:highlights_omitted")
        return omit_note

    if "digest:flags_omitted" not in report.missing:
        report.missing.append("digest:flags_omitted")
    return _join(no_flags, "_(omitted: flags — over digest size limit)_")


# Screening batch size budget (chars).  Cut at integration boundaries.
_SCREENING_BATCH_CHARS = 6000


_DOMAIN_RECOVERY_MIN_RATIO = 0.85


def _recover_screening_domain(report: "CoreReleaseReport", text: str) -> str:
    """Return the changelog domain whose entry best matches *text*, or ""."""
    import difflib

    needle = " ".join(text.lower().split())
    if not needle:
        return ""
    best_domain, best_ratio = "", 0.0
    for source in (report.changes_by_domain, report.patch_changes_by_domain):
        for domain, entries in source.items():
            for entry in entries:
                hay = " ".join(entry.text.lower().split())
                if needle in hay:
                    return domain
                ratio = difflib.SequenceMatcher(None, needle, hay).ratio()
                if ratio > best_ratio:
                    best_domain, best_ratio = domain, ratio
    return best_domain if best_ratio >= _DOMAIN_RECOVERY_MIN_RATIO else ""


def coerce_screening_items(
    report: "CoreReleaseReport", flags: list, highlights: list
) -> tuple[list[ScreeningFlag], list[ScreeningHighlight], int]:
    """Build screening models from raw LLM items; never raises.

    The model sometimes omits ``domain``.  It is recovered by matching the item's
    text against the report's changelog entries; items that still fail validation
    are dropped.  Returns (flags, highlights, dropped_count).
    """
    from pydantic import ValidationError

    def _build(items: list, model: type, text_key: str) -> tuple[list, int]:
        built: list = []
        dropped = 0
        for item in items:
            if isinstance(item, model):
                built.append(item)
                continue
            if not isinstance(item, dict):
                dropped += 1
                continue
            data = dict(item)
            if not data.get("domain"):
                data["domain"] = _recover_screening_domain(
                    report, str(data.get(text_key) or "")
                )
            try:
                if not data["domain"]:
                    raise ValueError("no domain")
                built.append(model(**data))
            except (ValidationError, ValueError, TypeError) as exc:
                dropped += 1
                log.warning(
                    "screening_item_dropped",
                    kind=model.__name__,
                    item=str(item)[:200],
                    error=str(exc)[:200],
                )
        return built, dropped

    out_flags, dropped_flags = _build(flags, ScreeningFlag, "change")
    out_hls, dropped_hls = _build(highlights, ScreeningHighlight, "feature")
    return out_flags, out_hls, dropped_flags + dropped_hls


def _section_parts(header: str, entry_lines: list[str]) -> list[str]:
    """Return one or more text parts for *header* + *entry_lines*.

    When the whole section fits within ``_SCREENING_BATCH_CHARS`` it is
    returned as a single string.  Otherwise entries are split at boundaries
    so each part is ≤ the limit; the header is repeated on each part so the
    LLM retains context about the domain.
    """
    full = header + "\n\n" + "\n".join(entry_lines)
    if len(full) <= _SCREENING_BATCH_CHARS:
        return [full]
    base_len = len(header) + 2  # header + "\n\n"
    parts: list[str] = []
    current: list[str] = []
    current_len = base_len
    for entry in entry_lines:
        entry_len = len(entry) + 1  # +1 for "\n"
        if current and current_len + entry_len > _SCREENING_BATCH_CHARS:
            parts.append(header + "\n\n" + "\n".join(current))
            current = []
            current_len = base_len
        current.append(entry)
        current_len += entry_len
    if current:
        parts.append(header + "\n\n" + "\n".join(current))
    return parts or [full]


def make_screening_batches(report: "CoreReleaseReport") -> list[str]:
    """Split the report's changelog content into LLM-sized batches.

    BC entries are excluded: the main analysis loop receives them verbatim via
    the release digest, so re-screening them wastes time and can produce false
    flags.  Only changelog sections (changes_by_domain) are batched.

    Each batch is a self-contained text block ≤ _SCREENING_BATCH_CHARS that
    cuts at entry boundaries.  A single domain with many entries is split into
    multiple parts, each repeating the domain header for context.  Returns a
    list of batch strings.
    """
    sections: list[str] = []

    # Changelog sections per domain only (no BC entries)
    for domain, entries in report.changes_by_domain.items():
        entry_lines = [f"- {e.text}" for e in entries]
        sections.extend(_section_parts(f"### Changes: {domain}", entry_lines))

    # Patch note sections — always included, even when the domain is also in the changelog
    for domain, entries in report.patch_changes_by_domain.items():
        entry_lines = [f"- {e.text}" for e in entries]
        sections.extend(_section_parts(f"### Patch changes: {domain}", entry_lines))

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


_BATCH_HEADER_RE = re.compile(r"^### (?:Changes|Patch changes): (.+)$", re.MULTILINE)


def batch_hash(text: str) -> str:
    """Return a short sha256 hex digest of *text* for use as a cache key."""
    return hashlib.sha256(text.encode()).hexdigest()


def batch_domains(text: str) -> list[str]:
    """Return the domain names appearing in *text*'s section headers."""
    return _BATCH_HEADER_RE.findall(text)


_REUSABLE_VERDICTS = frozenset({"action_needed", "no_action", "user_check"})


def load_stored_core_report(
    path: Path, version: str, installed_version: str
) -> Optional["CoreReleaseReport"]:
    """Return the report stored at *path* when it covers the same update, else None.

    Must be called before ``build_core_release_report``, which overwrites the
    file.  A missing or unreadable file, or one built for another target or
    installed version, returns None (#1221).
    """
    try:
        if not path.exists():
            return None
        stored = CoreReleaseReport.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("core_release_report_stored_unreadable", error=str(exc)[:200])
        return None
    if stored.version != version or stored.installed_version != installed_version:
        log.info(
            "core_release_report_stored_stale",
            stored_version=stored.version,
            stored_installed=stored.installed_version,
            version=version,
            installed=installed_version,
        )
        return None
    return stored


def load_stored_core_report_any_patch(
    report_dir: Path,
    version: str,
    installed_version: str,
) -> Optional["CoreReleaseReport"]:
    """Return the newest same-month report that shares *installed_version*.

    When an exact ``core_<version>.json`` is absent (e.g. HA moved from .1 to
    .2 before the user approved), scan *report_dir* for ``core_<YYYY.MM.*>.json``
    files with the same ``installed_version``.  Returns the most recently
    modified file that parses successfully, or None.  Used as a fallback after
    ``load_stored_core_report`` so the caller gets per-batch screening reuse
    and carried BC verdicts across a patch bump.
    """
    try:
        month_prefix = ".".join(version.split(".")[:2])  # "2026.11"
    except Exception:  # nosec B110
        return None
    try:
        candidates = sorted(
            report_dir.glob(f"core_{month_prefix}.*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:  # nosec B110
        return None
    for path in candidates:
        try:
            stored = CoreReleaseReport.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception:  # nosec B112
            continue
        if stored.installed_version != installed_version:
            continue
        if stored.version == version:
            # Exact match — the main loader should have caught this.
            continue
        log.info(
            "core_release_report_cross_patch_reuse",
            stored_version=stored.version,
            target_version=version,
            installed_version=installed_version,
        )
        return stored
    return None


def carry_over_screening(prev: "CoreReleaseReport", new: "CoreReleaseReport") -> bool:
    """Copy *prev*'s screening results into *new* on a per-batch basis.

    For each batch in *new*, if its sha256 matches a result stored in *prev*,
    that result is reused and the batch is skipped.  When *every* batch is
    covered this way the full pass is considered complete and the caller can
    skip the screening sessions entirely (returns True).  When only some batches
    are covered, copies those results into *new* and returns False so the caller
    runs only the remaining batches (#1221, amended).

    Backward compat: old reports with ``screening_complete=True`` but no
    ``screening_batch_results`` fall back to the previous all-or-nothing check.
    """
    if not prev.screening_batch_results:
        # Old report format: all-or-nothing
        if not prev.screening_complete:
            return False
        if make_screening_batches(prev) != make_screening_batches(new):
            return False
        new.screening_flags = list(prev.screening_flags)
        new.screening_highlights = list(prev.screening_highlights)
        new.screening_complete = True
        new.screening_batches_total = prev.screening_batches_total
        new.screening_batches_done = prev.screening_batches_done
        return True
    new_batches = make_screening_batches(new)
    if not new_batches:
        return False
    all_covered = True
    carried_batch_results: dict[str, dict] = {}
    for batch_text in new_batches:
        h = batch_hash(batch_text)
        if h in prev.screening_batch_results:
            carried_batch_results[h] = prev.screening_batch_results[h]
        else:
            all_covered = False
    if not carried_batch_results:
        return False
    # Merge carried results into new report
    new.screening_batch_results = dict(carried_batch_results)
    if all_covered:
        new.screening_flags = list(prev.screening_flags)
        new.screening_highlights = list(prev.screening_highlights)
        new.screening_complete = True
        new.screening_batches_total = prev.screening_batches_total
        new.screening_batches_done = prev.screening_batches_done
        return True
    return False


def carry_over_bc_verdicts(
    prev: "CoreReleaseReport", new: "CoreReleaseReport"
) -> list[dict]:
    """Return *prev*'s BC verdicts that still apply to *new*.

    A verdict is kept only when everything the assessment session saw is
    unchanged: the BC text, whether it is an installed-integration BC, and the
    evidence lines built for this run.  ``not_assessed`` is never kept (#1221).
    """

    def _inputs(report: "CoreReleaseReport") -> dict[str, tuple]:
        out: dict[str, tuple] = {}
        for installed, entries in (
            (True, report.bc_installed),
            (False, report.bc_general),
        ):
            for e in entries:
                ev = report.bc_evidence.get(e.name)
                out[e.name] = (
                    e.body,
                    installed,
                    tuple(_evidence_lines(ev)) if ev is not None else (),
                )
        return out

    prev_inputs = _inputs(prev)
    new_inputs = _inputs(new)
    kept: list[dict] = []
    seen: set[str] = set()
    for v in prev.bc_verdicts:
        name = v.get("bc")
        if name in seen or name not in new_inputs:
            continue
        if v.get("verdict") not in _REUSABLE_VERDICTS:
            continue
        if prev_inputs.get(name) != new_inputs[name]:
            continue
        seen.add(name)
        kept.append(dict(v))
    return kept


def carry_over_pueo_verdicts(
    prev: "CoreReleaseReport", new: "CoreReleaseReport"
) -> list[dict]:
    """Return *prev*'s pueo_impact verdicts that still apply to *new*.

    A verdict is kept only when the set of snippets for that dependency is
    unchanged between the two reports.  This covers the case where a re-run
    re-fetches identical notes — the session is skipped and the old verdict
    is reused.
    """

    def _snippets(report: "CoreReleaseReport") -> dict[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {}
        for h in report.pueo_dependency_hits:
            out.setdefault(h.dependency, []).append(h.snippet)
        return {dep: tuple(sorted(snips)) for dep, snips in out.items()}

    prev_snips = _snippets(prev)
    new_snips = _snippets(new)
    kept: list[dict] = []
    seen: set[str] = set()
    for v in prev.pueo_verdicts:
        dep = v.get("dependency")
        if dep in seen or dep not in new_snips:
            continue
        # dev_check verdicts are never reused — they indicate uncertainty and
        # should be re-evaluated each run.
        if v.get("verdict") == "dev_check":
            continue
        if prev_snips.get(dep) != new_snips.get(dep):
            continue
        seen.add(dep)
        kept.append(dict(v))
    return kept


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
