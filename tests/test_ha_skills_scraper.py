"""Tests for the HA best-practices skills scraper."""

from __future__ import annotations

from pathlib import Path

import pytest

from utils.knowledge.ha_skills_scraper import (
    _cache_is_stale,
    _chunk_markdown,
    embed_cached_ha_skills,
)
from utils.knowledge.knowledge_store import (
    COLLECTIONS,
    FakeKnowledgeStore,
    _authority_score,
)
from utils.agent.tool_executor import ToolExecutor


class TestChunkMarkdown:
    def test_splits_on_h2_headings(self):
        text = "Intro paragraph.\n\n## Section One\nContent one.\n\n## Section Two\nContent two."
        ids, docs, metas = _chunk_markdown(text, "ha_skills/SKILL", "best_practices")
        assert len(docs) >= 2
        assert any("Content one" in d for d in docs)
        assert any("Content two" in d for d in docs)

    def test_single_block_no_heading(self):
        text = "Just one block with no headings."
        ids, docs, metas = _chunk_markdown(
            text, "ha_skills/backups", "best_practices_ref"
        )
        assert len(docs) == 1
        assert ids[0].startswith("ha-skills-")

    def test_ids_prefixed_ha_skills(self):
        text = "## A\nfoo\n\n## B\nbar"
        ids, docs, metas = _chunk_markdown(text, "ha_skills/SKILL", "best_practices")
        for chunk_id in ids:
            assert chunk_id.startswith("ha-skills-")

    def test_chunk_truncated_at_3000(self):
        long_content = "x" * 5000
        ids, docs, metas = _chunk_markdown(
            long_content, "ha_skills/SKILL", "best_practices"
        )
        assert len(docs) == 1
        assert len(docs[0]) <= 3000

    def test_empty_text_returns_empty(self):
        ids, docs, metas = _chunk_markdown("", "ha_skills/SKILL", "best_practices")
        assert ids == []
        assert docs == []
        assert metas == []

    def test_metadata_contains_source_and_doc_type(self):
        text = "## Section\nSome content."
        ids, docs, metas = _chunk_markdown(text, "ha_skills/SKILL", "best_practices")
        assert metas[0]["source"] == "ha_skills/SKILL"
        assert metas[0]["doc_type"] == "best_practices"

    def test_ref_file_gets_best_practices_ref_doc_type(self):
        text = "## Safe refactoring\nDo this."
        ids, docs, metas = _chunk_markdown(
            text, "ha_skills/safe-refactoring", "best_practices_ref"
        )
        assert metas[0]["doc_type"] == "best_practices_ref"


class TestEmbedCachedHaSkills:
    def test_empty_cache_dir_nonexistent_returns_zero(self, tmp_path):
        store = FakeKnowledgeStore()
        result = embed_cached_ha_skills(str(tmp_path / "nonexistent"), store)
        assert result == 0

    def test_empty_cache_dir_no_files_returns_zero(self, tmp_path):
        store = FakeKnowledgeStore()
        result = embed_cached_ha_skills(str(tmp_path), store)
        assert result == 0

    def test_upserts_to_ha_best_practices_collection(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("## Best Practices\nAlways do backups.")
        store = FakeKnowledgeStore()
        embed_cached_ha_skills(str(tmp_path), store)
        assert store.collection_count("ha_best_practices") > 0

    def test_collected_ids_populated(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("## Section\nContent here.")
        store = FakeKnowledgeStore()
        ids: set[str] = set()
        embed_cached_ha_skills(str(tmp_path), store, collected_ids=ids)
        assert len(ids) > 0
        for chunk_id in ids:
            assert chunk_id.startswith("ha-skills-")

    def test_returns_count_of_files_processed(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("## Section\nContent.")
        (tmp_path / "backups.md").write_text("## Backups\nKeep them.")
        store = FakeKnowledgeStore()
        result = embed_cached_ha_skills(str(tmp_path), store)
        assert result == 2

    def test_skips_files_with_no_content(self, tmp_path):
        (tmp_path / "empty.md").write_text("")
        store = FakeKnowledgeStore()
        result = embed_cached_ha_skills(str(tmp_path), store)
        assert result == 0


class TestCacheIsStale:
    def test_missing_file_is_stale(self, tmp_path):
        assert _cache_is_stale(tmp_path / "missing.md", max_age_seconds=3600) is True

    def test_fresh_file_is_not_stale(self, tmp_path):
        p = tmp_path / "fresh.md"
        p.write_text("content")
        # File was just written; it cannot be older than 1 s.
        assert _cache_is_stale(p, max_age_seconds=1000) is False

    def test_old_file_is_stale(self, tmp_path, monkeypatch):
        import time as _time

        p = tmp_path / "old.md"
        p.write_text("content")
        # Pretend time has advanced by 200 hours
        fake_now = _time.time() + 200 * 3600
        monkeypatch.setattr(
            "utils.knowledge.ha_skills_scraper.time.time", lambda: fake_now
        )
        assert _cache_is_stale(p, max_age_seconds=168 * 3600) is True

    def test_zero_age_always_stale(self, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("x")
        assert _cache_is_stale(p, max_age_seconds=0) is True


class TestKnowledgeStoreIntegration:
    def test_ha_best_practices_in_collections(self):
        assert "ha_best_practices" in COLLECTIONS

    def test_authority_score_ha_best_practices(self):
        assert _authority_score("ha_best_practices", {}) == 0.9

    def test_authority_score_unchanged_for_others(self):
        assert _authority_score("repair_history", {}) == 0.5
        assert _authority_score("ha_integration_docs", {}) == 1.0


class TestToolExecutorLabels:
    def test_knowledge_authority_label_ha_best_practices(self):
        label = ToolExecutor._knowledge_authority_label("ha_best_practices", {})
        assert label == "[BEST PRACTICE]"

    def test_ha_best_practices_in_diagnostic_collections(self):
        assert "ha_best_practices" in ToolExecutor._QUERY_TYPE_COLLECTIONS["diagnostic"]

    def test_ha_best_practices_in_procedural_collections(self):
        assert "ha_best_practices" in ToolExecutor._QUERY_TYPE_COLLECTIONS["procedural"]

    def test_ha_best_practices_in_generative_collections(self):
        assert "ha_best_practices" in ToolExecutor._QUERY_TYPE_COLLECTIONS["generative"]

    def test_ha_best_practices_not_in_version_check(self):
        assert (
            "ha_best_practices"
            not in ToolExecutor._QUERY_TYPE_COLLECTIONS["version_check"]
        )


class TestConfig:
    def test_ha_skills_cache_dir_default_ends_with_ha_skills(self):
        import config

        assert config.HA_SKILLS_CACHE_DIR.endswith("ha_skills")

    def test_ha_skills_cache_dir_override(self, monkeypatch, tmp_path):
        custom = str(tmp_path / "custom_skills")
        import config as _config

        monkeypatch.setattr(_config, "HA_SKILLS_CACHE_DIR", custom)
        assert _config.HA_SKILLS_CACHE_DIR == custom
