"""Local RAG knowledge store wrapping ChromaDB (item 49)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


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


COLLECTIONS: tuple[str, ...] = (
    "ha_release_notes",
    "hacs_changelogs",
    "ha_integration_docs",
    "community_cases",
    "strategies",
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
                    results.append(
                        KnowledgeChunk(
                            text=doc,
                            source=meta.get("source", ""),
                            collection=col,
                            score=1.0,
                            metadata=meta,
                        )
                    )
        return results[:top_k]

    def prune(self, collection: str, keep_ids: set[str]) -> int:
        if collection not in self._docs:
            return 0
        before = len(self._docs[collection])
        self._docs[collection] = [d for d in self._docs[collection] if d[0] in keep_ids]
        return before - len(self._docs[collection])

    def total_count(self) -> int:
        return sum(len(docs) for docs in self._docs.values())


class ChromaKnowledgeStore:  # pragma: no cover
    """Production ChromaDB-backed knowledge store with Ollama embeddings."""

    def __init__(
        self,
        path: str,
        embed_model: str,
        ollama_endpoint: str,
        chroma_client=None,
    ) -> None:
        import chromadb

        self._client = chroma_client or chromadb.PersistentClient(path=path)
        ef = _OllamaEmbeddingFunction(embed_model, ollama_endpoint)
        self._cols = {
            name: self._client.get_or_create_collection(name, embedding_function=ef)  # type: ignore[arg-type]
            for name in COLLECTIONS
        }

    def upsert(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        self._cols[collection].upsert(ids=ids, documents=documents, metadatas=metadatas)  # type: ignore[arg-type]

    def query(
        self,
        query_text: str,
        top_k: int,
        collections: Optional[list[str]] = None,
        where: Optional[dict] = None,
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
            for doc, meta, dist in zip(docs, metas, dists):
                results.append(
                    KnowledgeChunk(
                        text=doc,
                        source=meta.get("source", ""),  # type: ignore[arg-type]
                        collection=col,
                        score=max(0.0, 1.0 - dist),
                        metadata=meta,  # type: ignore[arg-type]
                    )
                )
        results.sort(key=lambda c: c.score, reverse=True)
        return results[:top_k]

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


async def embed_local_episode(
    episode: Any,
    knowledge_store: Any,
    db_path: str,
) -> bool:
    """Embed a successful repair episode into the community_cases ChromaDB collection.

    Uses the episode ID as the ChromaDB document ID so repeated calls are idempotent
    (upsert semantics). Marks the episode as embedded in SQLite on success.
    Returns True on success, False on any error.
    """
    from utils.repair.repair_episode import (
        format_episode_for_embedding,
        mark_episode_embedded,
    )

    try:
        text = format_episode_for_embedding(episode)
        knowledge_store.upsert(
            collection="community_cases",
            ids=[episode.id],
            documents=[text],
            metadatas=[{"source": "local_episode", "episode_id": episode.id}],
        )
        mark_episode_embedded(db_path, episode.id)
        return True
    except Exception:
        return False


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
