"""Tests for utils/knowledge/ha_concepts_scraper.py."""

from utils.knowledge.ha_concepts_scraper import _CONCEPT_DOCS, parse_concept_doc


class TestConceptDocsList:
    def test_no_lovelace_paths(self):
        """Regression: lovelace/ paths were renamed to dashboards/ and _dashboards/ in HA docs repo."""
        lovelace_entries = [
            (doc_id, path)
            for doc_id, path in _CONCEPT_DOCS
            if path.startswith("lovelace/")
        ]
        assert (
            lovelace_entries == []
        ), f"Found stale lovelace/ paths (should be dashboards/ or _dashboards/): {lovelace_entries}"

    def test_dashboard_paths_present(self):
        paths = [path for _, path in _CONCEPT_DOCS]
        assert any(p.startswith("dashboards/") for p in paths)
        assert any(p.startswith("_dashboards/") for p in paths)


class TestParseConceptDoc:
    def test_strips_frontmatter(self):
        text = "---\ntitle: Test\n---\n# Hello\nsome content"
        chunks = parse_concept_doc(text)
        assert chunks
        assert all("title:" not in c for c in chunks)

    def test_empty_doc_returns_empty(self):
        assert parse_concept_doc("") == []

    def test_frontmatter_only_returns_empty(self):
        assert parse_concept_doc("---\ntitle: Only frontmatter\n---\n") == []

    def test_long_section_truncated(self):
        long_section = "word " * 700  # ~3500 chars
        chunks = parse_concept_doc(long_section)
        assert len(chunks) == 1
        assert len(chunks[0]) <= 3000

    def test_multiple_sections_split(self):
        text = "---\ntitle: T\n---\n# Section One\nfoo\n# Section Two\nbar"
        chunks = parse_concept_doc(text)
        assert len(chunks) == 2
