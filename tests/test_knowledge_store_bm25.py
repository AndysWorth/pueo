"""Tests for hybrid BM25+cosine retrieval in ChromaKnowledgeStore internals.

These tests exercise the BM25 logic using FakeKnowledgeStore where applicable
and the _bm25_scores_for_collection / _rebuild_bm25 helpers via a lightweight
mock of ChromaKnowledgeStore that avoids the real ChromaDB/Ollama dependencies.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from utils.knowledge.knowledge_store import (
    COLLECTIONS,
    FakeKnowledgeStore,
    KnowledgeChunk,
    _authority_score,
)


# ---------------------------------------------------------------------------
# FakeKnowledgeStore sanity checks (unchanged contract)
# ---------------------------------------------------------------------------


class TestFakeKnowledgeStoreUnchanged:
    """FakeKnowledgeStore should be unaffected by the BM25 addition."""

    def test_upsert_and_keyword_query(self):
        store = FakeKnowledgeStore()
        store.upsert(
            "ha_concepts",
            ["id1"],
            ["mqtt broker configuration"],
            [{"source": "concepts"}],
        )
        results = store.query("mqtt", top_k=5)
        assert len(results) == 1
        assert results[0].text == "mqtt broker configuration"

    def test_no_match_returns_empty(self):
        store = FakeKnowledgeStore()
        store.upsert(
            "ha_concepts",
            ["id1"],
            ["zigbee device pairing"],
            [{"source": "concepts"}],
        )
        results = store.query("mqtt", top_k=5)
        assert results == []

    def test_authority_score_applied(self):
        store = FakeKnowledgeStore()
        store.upsert(
            "ha_concepts",
            ["id1"],
            ["mqtt official doc"],
            [{"source": "official"}],
        )
        store.upsert(
            "strategies",
            ["id2"],
            ["mqtt candidate runbook"],
            [{"source": "agent_loop", "runbook_type": "candidate"}],
        )
        results = store.query("mqtt", top_k=5)
        # ha_concepts has authority 1.0 (official); strategies candidate is 0.6
        official = next(r for r in results if r.collection == "ha_concepts")
        candidate = next(r for r in results if r.collection == "strategies")
        assert official.authority_score > candidate.authority_score


# ---------------------------------------------------------------------------
# BM25 index internals (using a lightweight mock of ChromaKnowledgeStore)
# ---------------------------------------------------------------------------


def _make_mock_store(hybrid_weight: float = 0.3):
    """Return a ChromaKnowledgeStore-like object with mocked ChromaDB internals."""
    # We import ChromaKnowledgeStore but patch its __init__ dependencies
    from utils.knowledge.knowledge_store import ChromaKnowledgeStore

    with (
        patch("chromadb.PersistentClient"),
        patch.object(
            ChromaKnowledgeStore, "_get_cosine_collection", return_value=MagicMock()
        ),
    ):
        store = ChromaKnowledgeStore.__new__(ChromaKnowledgeStore)
        store._hybrid_weight = max(0.0, min(1.0, hybrid_weight))
        store._bm25_index = {}
        store._bm25_ids = {}
        # Each collection gets a MagicMock col object
        store._cols = {name: MagicMock() for name in COLLECTIONS}
    return store


class TestBM25IndexBuilding:
    def test_rebuild_bm25_populates_index(self):
        pytest.importorskip("rank_bm25")
        store = _make_mock_store()
        col = store._cols["ha_concepts"]
        col.get.return_value = {
            "documents": ["mqtt broker config", "zha device pairing"],
            "ids": ["id1", "id2"],
        }
        store._rebuild_bm25("ha_concepts")
        assert "ha_concepts" in store._bm25_index
        assert store._bm25_ids["ha_concepts"] == ["id1", "id2"]

    def test_rebuild_bm25_empty_collection(self):
        pytest.importorskip("rank_bm25")
        store = _make_mock_store()
        col = store._cols["ha_concepts"]
        col.get.return_value = {"documents": [], "ids": []}
        # Pre-populate to confirm it gets cleared
        store._bm25_index["ha_concepts"] = object()
        store._bm25_ids["ha_concepts"] = ["stale"]
        store._rebuild_bm25("ha_concepts")
        assert "ha_concepts" not in store._bm25_index
        assert "ha_concepts" not in store._bm25_ids

    def test_upsert_invalidates_bm25_index(self):
        pytest.importorskip("rank_bm25")
        store = _make_mock_store()
        # Seed a fake index entry
        store._bm25_index["ha_concepts"] = object()
        store._bm25_ids["ha_concepts"] = ["id_old"]
        # Upsert should clear the index
        store.upsert("ha_concepts", ["id1"], ["doc text"], [{"source": "s"}])
        assert "ha_concepts" not in store._bm25_index
        assert "ha_concepts" not in store._bm25_ids


class TestBM25Scoring:
    def test_exact_keyword_scores_higher_than_zero(self):
        pytest.importorskip("rank_bm25")
        store = _make_mock_store(hybrid_weight=1.0)
        col = store._cols["ha_concepts"]
        # BM25 IDF requires the query term to appear in fewer than half the docs.
        # Use a larger corpus so "mqtt" is rare (1/5 docs) and gets positive IDF.
        col.get.return_value = {
            "documents": [
                "mqtt broker configuration host port",
                "zigbee device pairing coordinator",
                "recorder database purge history",
                "template sensor binary_sensor state",
                "automation trigger condition action",
            ],
            "ids": ["mqtt_doc", "zha_doc", "recorder_doc", "template_doc", "auto_doc"],
        }
        scores = store._bm25_scores_for_collection("ha_concepts", "mqtt broker")
        assert scores["mqtt_doc"] > 0.0
        # zha doc should not contain mqtt; mqtt_doc must be highest scorer
        assert scores["mqtt_doc"] >= scores.get("zha_doc", 0.0)

    def test_all_zero_bm25_returns_empty(self):
        pytest.importorskip("rank_bm25")
        store = _make_mock_store()
        col = store._cols["ha_concepts"]
        col.get.return_value = {
            "documents": ["zigbee pairing"],
            "ids": ["id1"],
        }
        # Query with tokens that don't appear in any doc → all zero → empty dict
        scores = store._bm25_scores_for_collection("ha_concepts", "xyzzy")
        assert scores == {}

    def test_normalised_scores_max_is_one(self):
        pytest.importorskip("rank_bm25")
        store = _make_mock_store()
        col = store._cols["ha_concepts"]
        col.get.return_value = {
            "documents": [
                "mqtt mqtt mqtt host",
                "mqtt broker",
                "zha zigbee pairing",
            ],
            "ids": ["id1", "id2", "id3"],
        }
        scores = store._bm25_scores_for_collection("ha_concepts", "mqtt")
        assert scores  # non-empty
        assert max(scores.values()) == pytest.approx(1.0, abs=1e-9)


class TestHybridWeightBehavior:
    """Test that hybrid_weight=0.0 degrades to cosine-only."""

    def test_zero_weight_skips_bm25(self):
        pytest.importorskip("rank_bm25")
        store = _make_mock_store(hybrid_weight=0.0)
        col = store._cols["ha_concepts"]
        # query() result from ChromaDB (one doc)
        col.query.return_value = {
            "documents": [["mqtt broker config"]],
            "metadatas": [[{"source": "official"}]],
            "distances": [[0.1]],  # cosine dist → sim = 0.9
            "ids": [["id1"]],
        }
        results = store.query("mqtt", top_k=5, collections=["ha_concepts"])
        assert len(results) == 1
        # With weight=0.0, score == cosine_sim = 1 - 0.1 = 0.9
        assert results[0].score == pytest.approx(0.9, abs=1e-6)
        # BM25 rebuild should never be called when weight=0
        col.get.assert_not_called()

    def test_nonzero_weight_blends_scores(self):
        pytest.importorskip("rank_bm25")
        store = _make_mock_store(hybrid_weight=0.5)
        col = store._cols["ha_concepts"]
        col.query.return_value = {
            "documents": [["mqtt broker config"]],
            "metadatas": [[{"source": "official"}]],
            "distances": [[0.0]],  # perfect cosine → sim = 1.0
            "ids": [["id1"]],
        }
        # Mock BM25 to return a known score (normalised to 0.6 for id1)
        store._bm25_index["ha_concepts"] = MagicMock()
        store._bm25_ids["ha_concepts"] = ["id1"]
        store._bm25_index["ha_concepts"].get_scores.return_value = [0.6]

        results = store.query("mqtt", top_k=5, collections=["ha_concepts"])
        assert len(results) == 1
        # blended = 1.0 * 0.5 + (0.6/0.6) * 0.5 = 0.5 + 0.5 = 1.0
        assert results[0].score == pytest.approx(1.0, abs=1e-6)

    def test_missing_rank_bm25_falls_back_to_cosine(self, monkeypatch):
        """When rank_bm25 is not installed, _rebuild_bm25 returns early and
        query() falls back to cosine-only (bm25_map is empty)."""
        store = _make_mock_store(hybrid_weight=0.3)
        col = store._cols["ha_concepts"]
        col.query.return_value = {
            "documents": [["mqtt doc"]],
            "metadatas": [[{"source": "s"}]],
            "distances": [[0.2]],
            "ids": [["id1"]],
        }
        # Make rank_bm25 unavailable inside _rebuild_bm25
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "rank_bm25":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        results = store.query("mqtt", top_k=5, collections=["ha_concepts"])
        assert len(results) == 1
        # No BM25 → pure cosine: 1 - 0.2 = 0.8, with hybrid_weight blending:
        # blended = 0.8 * 0.7 + 0.0 * 0.3 = 0.56
        assert results[0].score == pytest.approx(0.56, abs=1e-6)


# ---------------------------------------------------------------------------
# Graceful empty-collection handling (hnsw error → [] not exception)
# ---------------------------------------------------------------------------


class TestGracefulEmptyCollection:
    """ChromaKnowledgeStore.query returns [] on hnsw error (Issue 3)."""

    def _make_chroma_store(self, failing_col: str):
        """Build a minimal ChromaKnowledgeStore with one collection that raises hnsw."""
        from unittest.mock import MagicMock

        import chromadb

        from utils.knowledge.knowledge_store import COLLECTIONS, ChromaKnowledgeStore

        # Build an in-memory chroma client with real collections
        client = chromadb.Client()

        class _HnswRaisingCollection:
            """Fake collection that raises an hnsw error on query()."""

            def query(self, **kwargs):
                raise RuntimeError("hnsw index not found (nothing found)")

            def get(self, **kwargs):
                return {"ids": [], "documents": [], "metadatas": []}

            def upsert(self, **kwargs):
                pass

            @property
            def metadata(self):
                return {"hnsw:space": "cosine"}

        embed_fn = MagicMock(return_value=[[0.0]])
        store = ChromaKnowledgeStore.__new__(ChromaKnowledgeStore)
        store._hybrid_weight = 0.0
        store._bm25_index = {}
        store._bm25_ids = {}
        store._cols = {name: _HnswRaisingCollection() for name in COLLECTIONS}
        return store

    def test_hnsw_error_returns_empty_list(self):
        """query() must return [] (not raise) when a collection throws an hnsw error."""
        import utils.knowledge.knowledge_store as ks_mod

        # Reset the flag before the test
        ks_mod._kb_needs_refresh = False

        store = self._make_chroma_store("ha_release_notes")
        results = store.query("test query", top_k=5)

        assert results == [], "hnsw error must return empty list, not propagate"
        assert (
            ks_mod._kb_needs_refresh
        ), "_kb_needs_refresh flag must be set after hnsw error"

    def test_non_hnsw_error_propagates(self):
        """Non-hnsw errors must still propagate (no silent swallowing of real errors)."""
        from unittest.mock import MagicMock

        from utils.knowledge.knowledge_store import COLLECTIONS, ChromaKnowledgeStore

        class _OtherErrorCollection:
            def query(self, **kwargs):
                raise ValueError("unexpected schema mismatch")

            def get(self, **kwargs):
                return {"ids": [], "documents": [], "metadatas": []}

            def upsert(self, **kwargs):
                pass

            @property
            def metadata(self):
                return {"hnsw:space": "cosine"}

        store = ChromaKnowledgeStore.__new__(ChromaKnowledgeStore)
        store._hybrid_weight = 0.0
        store._bm25_index = {}
        store._bm25_ids = {}
        store._cols = {name: _OtherErrorCollection() for name in COLLECTIONS}

        with pytest.raises(ValueError, match="unexpected schema"):
            store.query("test query", top_k=5)

    def test_get_kb_needs_refresh_reflects_flag(self):
        """get_kb_needs_refresh() returns the current flag value."""
        import utils.knowledge.knowledge_store as ks_mod
        from utils.knowledge.knowledge_store import get_kb_needs_refresh

        ks_mod._kb_needs_refresh = False
        assert not get_kb_needs_refresh()

        ks_mod._kb_needs_refresh = True
        assert get_kb_needs_refresh()

        ks_mod._kb_needs_refresh = False  # clean up
