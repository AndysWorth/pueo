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
