"""Integration tests: ChromaDB embedding function interface (regression for PR #86).

These tests verify _OllamaEmbeddingFunction implements all methods required by
the ChromaDB EmbeddingFunction ABC, using an in-memory EphemeralClient so no
disk I/O or Ollama network calls are needed.
"""

import pytest


def _fake_embed(monkeypatch, ef):
    """Patch _OllamaEmbeddingFunction._embed to return a fixed vector without Ollama."""
    fixed = [[0.1, 0.2, 0.3]]

    def _embed(self, texts):
        return [fixed[0] for _ in texts]

    monkeypatch.setattr(type(ef), "_embed", _embed)
    return fixed


class TestOllamaEmbeddingFunction:
    """Structural tests for the three ABC methods added in PR #86."""

    @pytest.fixture
    def ef(self):
        from utils.knowledge.knowledge_store import _OllamaEmbeddingFunction

        return _OllamaEmbeddingFunction(
            model="nomic-embed-text", endpoint="http://localhost:11434"
        )

    def test_name_returns_non_empty_string(self, ef):
        result = ef.name()
        assert isinstance(result, str)
        assert len(result) > 0
        assert "nomic-embed-text" in result

    def test_embed_documents_returns_list_of_vectors(self, monkeypatch, ef):
        _fake_embed(monkeypatch, ef)
        result = ef.embed_documents(["hello world"])
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], list)
        assert all(isinstance(v, float) for v in result[0])

    def test_embed_query_returns_list_of_vectors(self, monkeypatch, ef):
        _fake_embed(monkeypatch, ef)
        result = ef.embed_query(["search query"])
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], list)

    def test_call_still_works_as_regression_guard(self, monkeypatch, ef):
        """__call__ was already present before PR #86; must continue to work."""
        _fake_embed(monkeypatch, ef)
        result = ef(["test"])
        assert result == ef.embed_documents(["test"])

    def test_embed_documents_and_embed_query_both_route_through_embed(
        self, monkeypatch, ef
    ):
        """Calling embed_documents and embed_query each invoke _embed exactly once."""
        call_log = []
        fixed = [[0.1, 0.2, 0.3]]

        def recording_embed(self, texts):
            call_log.append(texts)
            return [fixed[0] for _ in texts]

        monkeypatch.setattr(type(ef), "_embed", recording_embed)

        ef.embed_documents(["doc"])
        ef.embed_query(["query"])

        assert len(call_log) == 2

    def test_chromadb_accepts_embedding_function_without_raising(self, monkeypatch, ef):
        """The exact regression test for PR #86: ChromaDB must not raise TypeError/NotImplementedError.

        Before the fix, missing name()/embed_documents()/embed_query() caused collection
        creation to fail with a ChromaDB validation error.
        """
        _fake_embed(monkeypatch, ef)

        try:
            import chromadb
        except ImportError:
            pytest.skip("chromadb not installed")

        client = chromadb.EphemeralClient()
        # This call was what raised before PR #86 was applied
        col = client.get_or_create_collection("test_regression", embedding_function=ef)
        assert col is not None


class TestChromeKnowledgeStoreRoundTrip:
    """End-to-end upsert → query through ChromaKnowledgeStore using EphemeralClient."""

    def test_upsert_then_query_returns_matching_chunk(self, monkeypatch):
        try:
            import chromadb
        except ImportError:
            pytest.skip("chromadb not installed")

        from utils.knowledge.knowledge_store import ChromaKnowledgeStore

        # Patch _embed to return a fixed vector
        from utils.knowledge.knowledge_store import _OllamaEmbeddingFunction

        def _embed(self, texts):
            return [[float(i) for i in range(16)] for _ in texts]

        monkeypatch.setattr(_OllamaEmbeddingFunction, "_embed", _embed)

        client = chromadb.EphemeralClient()
        store = ChromaKnowledgeStore(
            path="",
            embed_model="nomic-embed-text",
            ollama_endpoint="http://localhost:11434",
            chroma_client=client,
        )

        store.upsert(
            collection="ha_release_notes",
            ids=["doc1"],
            documents=["Breaking change: template syntax updated in 2025.7"],
            metadatas=[{"source": "ha_release_2025.7", "version": "2025.7"}],
        )

        results = store.query(
            "template syntax", top_k=1, collections=["ha_release_notes"]
        )
        assert isinstance(results, list)
        assert len(results) >= 1
        assert (
            "template" in results[0].text.lower()
            or results[0].source == "ha_release_2025.7"
        )
        assert results[0].score > 0.0, "score must be > 0 with cosine distance"


class TestChromeKnowledgeStoreCosineMigration:
    """Verify cosine-space migration logic in ChromaKnowledgeStore.__init__."""

    @staticmethod
    def _make_store(client, monkeypatch):
        from utils.knowledge.knowledge_store import (
            ChromaKnowledgeStore,
            _OllamaEmbeddingFunction,
        )

        def _embed(self, texts):
            return [[float(i) for i in range(16)] for _ in texts]

        monkeypatch.setattr(_OllamaEmbeddingFunction, "_embed", _embed)
        return ChromaKnowledgeStore(
            path="",
            embed_model="nomic-embed-text",
            ollama_endpoint="http://localhost:11434",
            chroma_client=client,
        )

    def test_collections_created_with_cosine_space(self, monkeypatch):
        try:
            import chromadb
        except ImportError:
            pytest.skip("chromadb not installed")
        from utils.knowledge.knowledge_store import COLLECTIONS

        client = chromadb.EphemeralClient()
        store = self._make_store(client, monkeypatch)
        for name in COLLECTIONS:
            col = client.get_collection(name)
            assert (col.metadata or {}).get(
                "hnsw:space"
            ) == "cosine", f"collection {name!r} should use cosine distance"
        assert store is not None

    def test_migration_resets_l2_collection_to_cosine(self, monkeypatch):
        try:
            import chromadb
        except ImportError:
            pytest.skip("chromadb not installed")

        client = chromadb.EphemeralClient()
        # Pre-create a collection with L2 (no metadata = ChromaDB default L2)
        client.create_collection("ha_release_notes")
        pre = client.get_collection("ha_release_notes")
        assert (pre.metadata or {}).get("hnsw:space") != "cosine"

        self._make_store(client, monkeypatch)

        post = client.get_collection("ha_release_notes")
        assert (post.metadata or {}).get("hnsw:space") == "cosine"
        assert post.count() == 0  # migrated collection starts empty

    def test_min_score_filter_works_with_cosine(self, monkeypatch):
        """Verify min_score=0.35 actually filters after the cosine fix."""
        try:
            import chromadb
        except ImportError:
            pytest.skip("chromadb not installed")
        from utils.knowledge.knowledge_store import (
            ChromaKnowledgeStore,
            _OllamaEmbeddingFunction,
        )

        call_count = [0]

        def _embed_varying(self, texts):
            # First call (upsert doc): identical vector → self-similarity score=1.0
            # Second call (query): slightly different → lower score
            call_count[0] += 1
            if call_count[0] == 1:
                return [[1.0] * 16 for _ in texts]
            # Query vector: orthogonal to stored → cosine distance = 1.0 → score = 0.0
            return [[0.0] * 15 + [1.0] for _ in texts]

        monkeypatch.setattr(_OllamaEmbeddingFunction, "_embed", _embed_varying)

        client = chromadb.EphemeralClient()
        store = ChromaKnowledgeStore(
            path="",
            embed_model="nomic-embed-text",
            ollama_endpoint="http://localhost:11434",
            chroma_client=client,
        )
        store.upsert(
            collection="ha_release_notes",
            ids=["doc1"],
            documents=["some content"],
            metadatas=[{"source": "test"}],
        )
        results = store.query(
            "query", top_k=1, collections=["ha_release_notes"], min_score=0.35
        )
        assert (
            results == []
        ), "orthogonal query should return no results above min_score=0.35"
