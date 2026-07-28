"""Local RAG knowledge store wrapping ChromaDB (item 49)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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
    "community_cases",
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
    ) -> list[KnowledgeChunk]:
        target_cols = collections or list(self._docs.keys())
        results: list[KnowledgeChunk] = []
        for col in target_cols:
            if col not in self._docs:
                continue
            for _, doc, meta in self._docs[col]:
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


class ChromaKnowledgeStore:  # pragma: no cover
    """Production ChromaDB-backed knowledge store with Ollama embeddings."""

    def __init__(self, path: str, embed_model: str, ollama_endpoint: str) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=path)
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
    ) -> list[KnowledgeChunk]:
        target_cols = collections or list(COLLECTIONS)
        results: list[KnowledgeChunk] = []
        for col in target_cols:
            if col not in self._cols:
                continue
            res = self._cols[col].query(query_texts=[query_text], n_results=top_k)
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


class _OllamaEmbeddingFunction:  # pragma: no cover
    """chromadb embedding function backed by Ollama."""

    def __init__(self, model: str, endpoint: str) -> None:
        self._model = model
        self._endpoint = endpoint

    def __call__(self, input: list[str]) -> list[list[float]]:  # type: ignore[override]
        import ollama

        client = ollama.Client(host=self._endpoint)
        return [
            client.embeddings(model=self._model, prompt=text)["embedding"]
            for text in input
        ]
