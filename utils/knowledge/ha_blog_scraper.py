"""HA blog scraper — fetches and parses HA monthly blog release notes (item 88).

GA releases on GitHub set the release body to a blog URL only.  This module
reads those cached STUB: files, fetches the real blog post, and overwrites the
file with the article text so scrape_cached_release_notes() can embed it.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

_BLOG_URL_RE = re.compile(r"https://www\.home-assistant\.io/blog/[^\s]+")
_STUB_THRESHOLD = 500

# Matches the "Backward-incompatible changes" (or "Breaking changes") heading.
_BC_SECTION_RE = re.compile(
    r"(?:^|\n)##\s+(?:Backward[- ]incompatible|Breaking)\s+changes?\b",
    re.IGNORECASE,
)


class _ArticleExtractor(HTMLParser):
    """Converts the <article> body to clean Markdown-like plain text."""

    def __init__(self) -> None:
        super().__init__()
        self._in_article = False
        self._tag_stack: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "article":
            self._in_article = True
        if not self._in_article:
            return
        self._tag_stack.append(tag)
        if tag == "h2":
            self._buf.append("\n\n## ")
        elif tag == "h3":
            self._buf.append("\n\n### ")
        elif tag == "h4":
            self._buf.append("\n\n#### ")
        elif tag == "summary":
            # <details>/<summary> blocks in BC section = one integration per entry
            self._buf.append("\n\n### ")
        elif tag == "p":
            self._buf.append("\n\n")
        elif tag == "li":
            self._buf.append("\n- ")

    def handle_endtag(self, tag: str) -> None:
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag == "article":
            self._in_article = False

    def handle_data(self, data: str) -> None:
        if self._in_article:
            text = data.strip()
            if text:
                self._buf.append(text + " ")

    def result(self) -> str:
        text = "".join(self._buf)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def extract_blog_url_from_stub(stub_text: str) -> Optional[str]:
    """Return the first home-assistant.io/blog URL found in stub_text, or None."""
    m = _BLOG_URL_RE.search(stub_text)
    return m.group(0) if m else None


def extract_breaking_changes(blog_text: str) -> list[tuple[str, str]]:
    """Return [(integration_name, description), ...] from the BC section.

    Relies on ``_ArticleExtractor`` having emitted ``### `` headings for each
    ``<details>/<summary>`` integration block.
    """
    m = _BC_SECTION_RE.search(blog_text)
    if not m:
        return []
    after = m.end()
    # Next top-level ## heading ends the BC section
    next_sec = re.search(r"\n##\s+(?!#)", blog_text[after:])
    sec_end = after + next_sec.start() if next_sec else len(blog_text)
    section = blog_text[m.start() : sec_end]

    results: list[tuple[str, str]] = []
    for entry in re.split(r"\n###\s+", section)[1:]:
        head, _, body = entry.partition("\n")
        name = head.strip()
        if name:
            results.append((name, body.strip()))
    return results


def format_core_notes_for_analysis(
    notes: str,
    installed_integrations: Optional[list[str]] = None,
) -> str:
    """Restructure Core release notes so installed-integration BCs appear first.

    Returns a string containing:
    (a) breaking changes for installed integrations, in full;
    (b) names of other affected integrations;
    (c) Farewell/removed section if present;
    (d) a leading highlights excerpt (capped at 6 000 chars).
    Total output is capped at 15 000 chars.
    """
    bc_entries = extract_breaking_changes(notes)
    if not bc_entries:
        return notes[:15000]

    _installed_set = set(installed_integrations or [])

    def _matches(heading: str) -> bool:
        normalized = re.sub(r"\s*\([^)]*\)", "", heading).lower().strip()
        key = re.sub(r"[\s\-]+", "_", normalized)
        if key in _installed_set:
            return True
        bare = key.replace("_", "")
        return any(dom.replace("_", "") == bare for dom in _installed_set)

    installed_bc: list[str] = []
    other_bc_names: list[str] = []
    for name, body in bc_entries:
        if _matches(name):
            installed_bc.append(f"### {name}\n\n{body}" if body else f"### {name}")
        else:
            other_bc_names.append(name)

    out_parts: list[str] = []

    if installed_bc or other_bc_names:
        bc_section = "## Backward-incompatible changes\n\n"
        if installed_bc:
            bc_section += (
                "**Affects this instance:**\n\n" + "\n\n".join(installed_bc) + "\n\n"
            )
        if other_bc_names:
            bc_section += (
                "**Other affected integrations (not installed here):** "
                + ", ".join(other_bc_names)
            )
        out_parts.append(bc_section)

    # Include Farewell/removed section
    farewell_m = re.search(r"\n##\s+Farewell\b", notes, re.IGNORECASE)
    if farewell_m:
        fw_start = farewell_m.start()
        next_fw = re.search(r"\n##\s+", notes[fw_start + 4 :])
        fw_end = fw_start + 4 + next_fw.start() if next_fw else len(notes)
        out_parts.append(notes[fw_start:fw_end].strip())

    # Leading highlights — everything before the BC or Farewell section
    highlights_end = len(notes)
    bc_m = _BC_SECTION_RE.search(notes)
    if bc_m:
        highlights_end = min(highlights_end, bc_m.start())
    if farewell_m:
        highlights_end = min(highlights_end, farewell_m.start())
    highlights = notes[:highlights_end].strip()
    if highlights:
        out_parts.insert(0, highlights[:6000])

    result = "\n\n---\n\n".join(out_parts)
    return result[:15000]


def fetch_blog_post(
    url: str, *, _fetcher: Optional[Callable[[str], bytes]] = None
) -> str:
    """Fetch a blog post and return the article body as plain text.

    The _fetcher injectable replaces the HTTP call in tests — it receives a
    URL and returns raw HTML bytes (or a str).
    """
    if _fetcher is not None:
        raw = _fetcher(url)
    else:  # pragma: no cover
        import urllib.request

        req = urllib.request.Request(
            url, headers={"User-Agent": "pueo-rag-refresh/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            raw = resp.read()

    html = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    parser = _ArticleExtractor()
    parser.feed(html)
    return parser.result()


def fetch_blog_release_notes(
    cache_dir: str, *, _fetcher: Optional[Callable[[str], bytes]] = None
) -> int:
    """Replace STUB: cache files with real blog content.

    Iterates *.txt files in cache_dir.  For each file whose content starts
    with ``STUB:``, extracts the embedded blog URL, fetches the article, and
    if the result is ≥ 500 chars, overwrites the file without the STUB: prefix.
    Returns the number of files successfully replaced.
    """
    path = Path(cache_dir)
    if not path.exists():
        return 0
    replaced = 0
    for fp in sorted(path.glob("*.txt")):
        try:
            content = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        if not content.startswith("STUB:"):
            continue
        stub_body = content[len("STUB:") :]
        url = extract_blog_url_from_stub(stub_body)
        if not url:
            continue
        try:
            real_content = fetch_blog_post(url, _fetcher=_fetcher)
        except Exception:  # nosec B112 — network errors skip to next file
            continue
        if len(real_content.strip()) >= _STUB_THRESHOLD:
            try:
                fp.write_text(real_content, encoding="utf-8")
                replaced += 1
            except OSError:
                continue
    return replaced
