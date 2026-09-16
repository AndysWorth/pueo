"""Local RAG knowledge store wrapping ChromaDB (item 49)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

_log = logging.getLogger("knowledge_store")


def _matches_where(meta: dict, where: Optional[dict]) -> bool:
    """Return True if meta satisfies the ChromaDB-style where clause."""
    if not where:
        return True
    for key, condition in where.items():
        value = meta.get(key)
        if isinstance(condition, dict):
            if "$in" in condition and value not in condition["$in"]:
                return False
            if "$eq" in condition and value != condition["$eq"]:
                return False
        elif value != condition:
            return False
    return True


@dataclass
class KnowledgeChunk:
    text: str
    source: str
    collection: str
    score: float = 0.0
    metadata: dict = field(default_factory=dict)
    authority_score: float = 0.0


def _authority_score(collection: str, metadata: dict) -> float:
    """Return a trust score [0,1] for a chunk based on its origin.

    Used to blend with cosine similarity so official docs rank above
    unreviewed community content when relevance is otherwise equal.
    """
    if collection in (
        "ha_integration_docs",
        "ha_concepts",
        "ha_release_notes",
        "ha_developer_docs",
    ):
        return 1.0
    if collection == "strategies":
        src = metadata.get("source", "")
        if src == "seed_prompt":
            return 0.8
        runbook_type = metadata.get("runbook_type", "")
        if runbook_type == "seed":
            return 0.8
        # pueo_kb runbooks that have been reviewed are community-grade
        if src == "pueo_kb":
            return 0.7
        return 0.6  # candidate / agent_learned
    if collection == "repair_history":
        return 0.5
    # hacs_changelogs and any future community collections
    return 0.6


COLLECTIONS: tuple[str, ...] = (
    "ha_release_notes",
    "hacs_changelogs",
    "ha_integration_docs",
    "ha_concepts",
    "ha_developer_docs",
    "strategies",
    "repair_history",
)


class FakeKnowledgeStore:
    """In-memory knowledge store for unit tests."""

    def __init__(self) -> None:
        self._docs: dict[str, list[tuple[str, str, dict]]] = {}

    def upsert(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        if collection not in self._docs:
            self._docs[collection] = []
        # Remove existing docs with the same ids before upserting
        new_ids = set(ids)
        self._docs[collection] = [
            d for d in self._docs[collection] if d[0] not in new_ids
        ]
        for doc_id, doc, meta in zip(ids, documents, metadatas):
            self._docs[collection].append((doc_id, doc, meta))

    def query(
        self,
        query_text: str,
        top_k: int,
        collections: Optional[list[str]] = None,
        where: Optional[dict] = None,
        min_score: float = 0.0,
    ) -> list[KnowledgeChunk]:
        target_cols = collections or list(self._docs.keys())
        results: list[KnowledgeChunk] = []
        for col in target_cols:
            if col not in self._docs:
                continue
            for _, doc, meta in self._docs[col]:
                if not _matches_where(meta, where):
                    continue
                if query_text.lower() in doc.lower():
                    auth = _authority_score(col, meta)
                    results.append(
                        KnowledgeChunk(
                            text=doc,
                            source=meta.get("source", ""),
                            collection=col,
                            score=1.0,
                            metadata=meta,
                            authority_score=auth,
                        )
                    )
        results.sort(
            key=lambda c: c.authority_score * 0.3 + c.score * 0.7, reverse=True
        )
        filtered = [c for c in results if c.score >= min_score]
        return filtered[:top_k]

    def prune(self, collection: str, keep_ids: set[str]) -> int:
        if collection not in self._docs:
            return 0
        before = len(self._docs[collection])
        self._docs[collection] = [d for d in self._docs[collection] if d[0] in keep_ids]
        return before - len(self._docs[collection])

    def total_count(self) -> int:
        return sum(len(docs) for docs in self._docs.values())

    def collection_count(self, name: str) -> int:
        return len(self._docs.get(name, []))


class ChromaKnowledgeStore:  # pragma: no cover
    """Production ChromaDB-backed knowledge store with Ollama embeddings."""

    def __init__(
        self,
        path: str,
        embed_model: str,
        ollama_endpoint: str,
        chroma_client=None,
        hybrid_weight: float = 0.3,
    ) -> None:
        import chromadb

        self._client = chroma_client or chromadb.PersistentClient(path=path)
        ef = _OllamaEmbeddingFunction(embed_model, ollama_endpoint)
        self._cols = {
            name: self._get_cosine_collection(name, ef) for name in COLLECTIONS
        }
        # BM25 hybrid retrieval: in-memory index per collection.
        # Each entry is (corpus, id_list) rebuilt lazily on upsert.
        self._hybrid_weight: float = max(0.0, min(1.0, hybrid_weight))
        self._bm25_index: dict[str, Any] = {}  # col -> BM25Okapi instance
        self._bm25_ids: dict[str, list[str]] = {}  # col -> ordered id list

    def _get_cosine_collection(self, name: str, ef: Any) -> Any:  # type: ignore[return]
        """Return a cosine-distance collection, migrating from L2 if necessary."""
        cosine_meta = {"hnsw:space": "cosine"}
        try:
            existing = self._client.get_collection(name)
            if (existing.metadata or {}).get("hnsw:space") != "cosine":
                _log.warning(
                    "kb_collection_migrating",
                    extra={
                        "collection": name,
                        "found": (existing.metadata or {}).get("hnsw:space"),
                    },
                )
                self._client.delete_collection(name)
        except Exception:  # nosec B110
            pass  # collection does not exist yet — normal on first run
        return self._client.get_or_create_collection(  # type: ignore[return-value]
            name,
            embedding_function=ef,  # type: ignore[arg-type]
            metadata=cosine_meta,
        )

    def _rebuild_bm25(self, collection: str) -> None:
        """Rebuild the BM25 index for *collection* from the current ChromaDB contents."""
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            return
        result = self._cols[collection].get()
        docs = result.get("documents") or []
        ids = result.get("ids") or []
        if not docs:
            self._bm25_index.pop(collection, None)
            self._bm25_ids.pop(collection, None)
            return
        tokenized = [doc.lower().split() for doc in docs]
        self._bm25_index[collection] = BM25Okapi(tokenized)
        self._bm25_ids[collection] = list(ids)

    def upsert(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        self._cols[collection].upsert(ids=ids, documents=documents, metadatas=metadatas)  # type: ignore[arg-type]
        # Invalidate BM25 index so it is rebuilt on next query.
        self._bm25_index.pop(collection, None)
        self._bm25_ids.pop(collection, None)

    def _bm25_scores_for_collection(
        self, collection: str, query_text: str
    ) -> dict[str, float]:
        """Return a {doc_id: normalised_bm25_score} mapping for *collection*.

        Scores are normalised to [0, 1] by dividing by the maximum raw score.
        Returns an empty dict when rank-bm25 is not installed or the index is empty.
        """
        if collection not in self._bm25_index:
            self._rebuild_bm25(collection)
        bm25 = self._bm25_index.get(collection)
        ids = self._bm25_ids.get(collection, [])
        if bm25 is None or not ids:
            return {}
        tokens = query_text.lower().split()
        raw_scores = bm25.get_scores(tokens)
        max_score = max(raw_scores) if len(raw_scores) > 0 else 0.0
        if max_score <= 0:
            return {}
        return {doc_id: float(s) / max_score for doc_id, s in zip(ids, raw_scores)}

    def query(
        self,
        query_text: str,
        top_k: int,
        collections: Optional[list[str]] = None,
        where: Optional[dict] = None,
        min_score: float = 0.0,
    ) -> list[KnowledgeChunk]:
        target_cols = collections or list(COLLECTIONS)
        results: list[KnowledgeChunk] = []
        for col in target_cols:
            if col not in self._cols:
                continue
            kwargs: dict[str, Any] = {"query_texts": [query_text], "n_results": top_k}
            if where:
                kwargs["where"] = where
            res = self._cols[col].query(**kwargs)
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            ids = (res.get("ids") or [[]])[0]
            bm25_map = (
                self._bm25_scores_for_collection(col, query_text)
                if self._hybrid_weight > 0
                else {}
            )
            for doc_id, doc, meta, dist in zip(ids, docs, metas, dists):
                cosine_sim = max(0.0, 1.0 - dist)  # cosine dist ∈ [0,2]; score ∈ [0,1]
                bm25_sim = bm25_map.get(doc_id, 0.0)
                # Blend: cosine weight = (1 - hybrid_weight), BM25 weight = hybrid_weight
                blended = (
                    cosine_sim * (1.0 - self._hybrid_weight)
                    + bm25_sim * self._hybrid_weight
                )
                auth = _authority_score(col, meta)  # type: ignore[arg-type]
                results.append(
                    KnowledgeChunk(
                        text=doc,
                        source=meta.get("source", ""),  # type: ignore[arg-type]
                        collection=col,
                        score=blended,
                        metadata=meta,  # type: ignore[arg-type]
                        authority_score=auth,
                    )
                )
        results.sort(
            key=lambda c: c.authority_score * 0.3 + c.score * 0.7, reverse=True
        )
        filtered = [c for c in results if c.score >= min_score]
        return filtered[:top_k]

    def prune(self, collection: str, keep_ids: set[str]) -> int:
        if collection not in self._cols:
            return 0
        col = self._cols[collection]
        all_ids = col.get()["ids"]
        stale = [id_ for id_ in all_ids if id_ not in keep_ids]
        if stale:
            col.delete(ids=stale)
        return len(stale)

    def total_count(self) -> int:
        return sum(col.count() for col in self._cols.values())

    def collection_count(self, name: str) -> int:
        if name not in self._cols:
            return 0
        return self._cols[name].count()


class _OllamaEmbeddingFunction:  # pragma: no cover
    """chromadb embedding function backed by Ollama."""

    def __init__(self, model: str, endpoint: str) -> None:
        self._model = model
        self._endpoint = endpoint

    def name(self) -> str:
        return f"ollama-{self._model}"

    def _embed(self, input: list[str]) -> list[list[float]]:
        import ollama

        client = ollama.Client(host=self._endpoint)
        return [
            client.embeddings(model=self._model, prompt=text)["embedding"]
            for text in input
        ]

    def __call__(self, input: list[str]) -> list[list[float]]:  # type: ignore[override]
        return self._embed(input)

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        return self._embed(input)

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self._embed(input)
