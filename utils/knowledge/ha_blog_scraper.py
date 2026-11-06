"""HA blog scraper — fetches and parses HA monthly blog release notes (item 88).

GA releases on GitHub set the release body to a blog URL only.  This module
reads those cached STUB: files, fetches the real blog post, and overwrites the
file with the article text so scrape_cached_release_notes() can embed it.
"""

from __future__ import annotations

import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

_BLOG_URL_RE = re.compile(r"https://www\.home-assistant\.io/blog/[^\s]+")
# Markdown link form: [text](url)  — preferred because it avoids trailing punctuation.
_BLOG_MD_LINK_RE = re.compile(r"\((https://www\.home-assistant\.io/blog/[^)\s]+)\)")
_STUB_THRESHOLD = 500

# Matches the "Backward-incompatible changes" (or "Breaking changes") heading.
_BC_SECTION_RE = re.compile(
    r"(?:^|\n)##\s+(?:Backward[- ]incompatible|Breaking)\s+changes?\b",
    re.IGNORECASE,
)
# Matches the [domains: X, Y] annotation emitted by _ArticleExtractor.
_DOMAIN_ANNOTATION_RE = re.compile(r"\[domains:\s*([^\]]+)\]")


class _GenericHtmlExtractor(HTMLParser):
    """Convert any HTML page's main content to plain text.

    Container priority: <article> > <main> > <body>.  Buffers each separately
    so the best non-empty container wins in ``result()``.  Skips
    nav/header/footer/aside/script/style regardless of container.

    ``start_id``: when set, output begins at the first element whose ``id``
    attribute matches (URL-decoded, case-insensitive).  Content seen before
    that element is discarded.
    """

    _SKIP_TAGS = frozenset(
        {"script", "style", "nav", "header", "footer", "aside", "noscript", "template"}
    )
    _HEADING_MAP = {
        "h1": "# ",
        "h2": "## ",
        "h3": "### ",
        "h4": "#### ",
        "h5": "##### ",
    }
    _CONTAINERS = ("article", "main", "body")

    def __init__(self, start_id: str = "") -> None:
        super().__init__()
        self._tag_stack: list[str] = []
        self._skip_depth = 0
        self._buffers: dict[str, list[str]] = {c: [] for c in self._CONTAINERS}
        self._active_containers: list[str] = []
        self._start_id = urllib.parse.unquote(start_id).lower()
        self._found_start = not bool(start_id)

    def _current_buf(self) -> Optional[list[str]]:
        return (
            self._buffers[self._active_containers[-1]]
            if self._active_containers
            else None
        )

    def _write(self, text: str) -> None:
        buf = self._current_buf()
        if buf is not None:
            buf.append(text)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self._tag_stack.append(tag)
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if tag in self._CONTAINERS:
            self._active_containers.append(tag)
            return
        if not self._active_containers:
            return
        if self._start_id:
            elem_id = urllib.parse.unquote(dict(attrs).get("id", "")).lower()
            if elem_id == self._start_id:
                self._found_start = True
                for buf in self._buffers.values():
                    buf.clear()
        if not self._found_start:
            return
        if tag in self._HEADING_MAP:
            self._write(f"\n\n{self._HEADING_MAP[tag]}")
        elif tag == "summary":
            self._write("\n\n### ")
        elif tag == "p":
            self._write("\n\n")
        elif tag == "li":
            self._write("\n- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if (
            tag in self._CONTAINERS
            and self._active_containers
            and self._active_containers[-1] == tag
        ):
            self._active_containers.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or not self._active_containers or not self._found_start:
            return
        text = data.strip()
        if text:
            self._write(text + " ")

    def result(self) -> str:
        for container in self._CONTAINERS:
            buf = self._buffers[container]
            if any(s.strip() for s in buf):
                text = "".join(buf)
                text = re.sub(r"\n{3,}", "\n\n", text)
                return text.strip()
        return ""


def html_to_text(html: str, fragment: str = "") -> str:
    """Extract the main content of an HTML page as plain text.

    Uses ``_GenericHtmlExtractor``.  ``fragment`` is the URL fragment (without
    ``#``); when non-empty, output starts at the element with that id.
    """
    extractor = _GenericHtmlExtractor(start_id=fragment)
    extractor.feed(html)
    return extractor.result()


class _ArticleExtractor(HTMLParser):
    """Converts the <article> body to clean Markdown-like plain text.

    Inside ``<details>`` blocks, tracks ``<a href="/integrations/<domain>/">``
    links whose text ends with "documentation" and emits a ``[domains: X]``
    annotation at the end of each block so ``extract_breaking_changes`` can
    match entries to their exact HA domain without relying on display-name
    normalisation alone.
    """

    _INTEGRATION_HREF_RE = re.compile(r"^/integrations/([^/]+)/$")

    def __init__(self) -> None:
        super().__init__()
        self._in_article = False
        self._tag_stack: list[str] = []
        self._buf: list[str] = []
        self._in_details = False
        self._details_domains: list[str] = []
        self._pending_a_domain: Optional[str] = None
        self._link_text_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "article":
            self._in_article = True
        if not self._in_article:
            return
        self._tag_stack.append(tag)
        if tag == "details":
            self._in_details = True
            self._details_domains = []
        elif tag == "a" and self._in_details:
            href = dict(attrs).get("href", "")
            m = self._INTEGRATION_HREF_RE.match(href)
            if m:
                self._pending_a_domain = m.group(1)
                self._link_text_buf = []
        elif tag == "h2":
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
        elif tag == "details" and self._in_details:
            if self._details_domains:
                self._buf.append(f"\n[domains: {', '.join(self._details_domains)}]")
            self._in_details = False
            self._details_domains = []
        elif tag == "a" and self._pending_a_domain is not None:
            link_text = "".join(self._link_text_buf).strip().lower()
            if link_text.endswith("documentation"):
                self._details_domains.append(self._pending_a_domain)
            self._pending_a_domain = None
            self._link_text_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_article:
            text = data.strip()
            if text:
                self._buf.append(text + " ")
                if self._pending_a_domain is not None:
                    self._link_text_buf.append(text)

    def result(self) -> str:
        text = "".join(self._buf)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def extract_blog_url_from_stub(stub_text: str) -> Optional[str]:
    """Return the first home-assistant.io/blog URL found in stub_text, or None.

    Prefers the target of a markdown link ``[text](url)`` so that trailing
    punctuation such as ``).`` in ``[release notes](url).`` is not included.
    Falls back to a bare URL match with trailing ``).,[...]>"'`` stripped.
    """
    m = _BLOG_MD_LINK_RE.search(stub_text)
    if m:
        return m.group(1)
    m = _BLOG_URL_RE.search(stub_text)
    if not m:
        return None
    url = m.group(0).rstrip(").,]>\"'")
    return url or None


def extract_breaking_changes(blog_text: str) -> list[tuple[str, list[str], str]]:
    """Return [(name, domains, description), ...] from the BC section.

    ``domains`` is populated from the ``[domains: X]`` annotation that
    ``_ArticleExtractor`` emits inside each ``<details>`` block when it finds
    a "… documentation" link.  It is an empty list for general entries (Entity
    names, Python dependencies) that have no associated integration.

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

    results: list[tuple[str, list[str], str]] = []
    for entry in re.split(r"\n###\s+", section)[1:]:
        head, _, body = entry.partition("\n")
        name = head.strip()
        if name:
            dm = _DOMAIN_ANNOTATION_RE.search(body)
            domains = (
                [d.strip() for d in dm.group(1).split(",") if d.strip()] if dm else []
            )
            clean_body = _DOMAIN_ANNOTATION_RE.sub("", body).strip()
            results.append((name, domains, clean_body))
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

    def _matches_domains(domains: list[str]) -> bool:
        return bool(_installed_set.intersection(domains))

    def _matches_name(heading: str) -> bool:
        normalized = re.sub(r"\s*\([^)]*\)", "", heading).lower().strip()
        key = re.sub(r"[\s\-]+", "_", normalized)
        if key in _installed_set:
            return True
        bare = key.replace("_", "")
        return any(dom.replace("_", "") == bare for dom in _installed_set)

    installed_bc: list[str] = []
    general_bc: list[str] = []
    other_bc_names: list[str] = []
    for name, domains, body in bc_entries:
        entry_text = f"### {name}\n\n{body}" if body else f"### {name}"
        if not domains:
            # No integration-doc link.  Try name matching; unmatched entries
            # surface as general (could be truly general like "Entity names",
            # or a plain-text entry without domain annotations).
            if _matches_name(name):
                installed_bc.append(entry_text)
            else:
                general_bc.append(entry_text)
        elif _matches_domains(domains) or _matches_name(name):
            installed_bc.append(entry_text)
        else:
            other_bc_names.append(name)

    out_parts: list[str] = []

    if installed_bc or general_bc or other_bc_names:
        bc_section = "## Backward-incompatible changes\n\n"
        if installed_bc:
            bc_section += (
                "**Affects this instance:**\n\n" + "\n\n".join(installed_bc) + "\n\n"
            )
        if general_bc:
            bc_section += (
                "**General (applies to all installs):**\n\n"
                + "\n\n".join(general_bc)
                + "\n\n"
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
