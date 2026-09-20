from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.http.models import (
    Distance,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from texponse.integrations.openrouter import OpenRouterClient

logger = logging.getLogger(__name__)


@dataclass
class RetrievedPassage:
    text: str
    score: float
    source: str
    metadata: dict[str, Any]


class Embedder:
    def __init__(self, openrouter_client: OpenRouterClient) -> None:
        self._client = openrouter_client
        self._model = "openai/text-embedding-3-small"

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return await self._client.embed(texts, model=self._model)

    async def embed_query(self, text: str) -> list[float]:
        embeddings = await self.embed_texts([text])
        return embeddings[0]

    @staticmethod
    def build_sparse_vector(text: str) -> SparseVector:
        import hashlib

        tokens = text.lower().split()
        token_set = sorted(set(tokens))
        index_to_value: dict[int, float] = {}
        for tok in token_set:
            idx = int(hashlib.md5(tok.encode()).hexdigest(), 16) % 100000
            index_to_value[idx] = index_to_value.get(idx, 0.0) + 1.0
        sorted_indices = sorted(index_to_value)
        return SparseVector(
            indices=sorted_indices,
            values=[index_to_value[i] for i in sorted_indices],
        )


class HybridRetriever:
    def __init__(
        self,
        qdrant_client: QdrantClient,
        embedder: Embedder,
        collection_name: str,
        top_k: int = 10,
    ) -> None:
        self._qdrant = qdrant_client
        self._embedder = embedder
        self._collection = collection_name
        self._top_k = top_k

    async def retrieve(
        self,
        query: str,
        filters: models.Filter | None = None,
    ) -> list[RetrievedPassage]:
        import asyncio

        dense_vector = await self._embedder.embed_query(query)
        sparse_vector = Embedder.build_sparse_vector(query)

        dense_response = await asyncio.to_thread(
            self._qdrant.query_points,
            self._collection,
            query=dense_vector,
            using="dense",
            query_filter=filters,
            with_payload=True,
            limit=self._top_k,
        )

        sparse_response = await asyncio.to_thread(
            self._qdrant.query_points,
            self._collection,
            query=sparse_vector,
            using="bm25",
            query_filter=filters,
            with_payload=True,
            limit=self._top_k,
        )

        passages = self._rrf_fusion(dense_response.points, sparse_response.points, k=60)
        return passages[: self._top_k]

    def _rrf_fusion(
        self,
        dense_results: list[Any],
        sparse_results: list[Any],
        k: int = 60,
    ) -> list[RetrievedPassage]:
        scores: dict[str, float] = {}
        source_map: dict[str, RetrievedPassage] = {}

        for rank, point in enumerate(dense_results):
            fid = str(point.id)
            score = 1.0 / (k + rank + 1)
            scores[fid] = scores.get(fid, 0.0) + score
            payload = point.payload or {}
            if fid not in source_map:
                source_map[fid] = RetrievedPassage(
                    text=payload.get("text", ""),
                    score=0.0,
                    source=payload.get("source_url", payload.get("source", "")),
                    metadata={
                        "category": payload.get("category", ""),
                        "last_updated": payload.get("last_updated", ""),
                        "chunk_index": payload.get("chunk_index", 0),
                    },
                )

        for rank, point in enumerate(sparse_results):
            fid = str(point.id)
            score = 1.0 / (k + rank + 1)
            scores[fid] = scores.get(fid, 0.0) + score

            payload = point.payload or {}
            if fid not in source_map:
                source_map[fid] = RetrievedPassage(
                    text=payload.get("text", ""),
                    score=0.0,
                    source=payload.get("source_url", payload.get("source", "")),
                    metadata={
                        "category": payload.get("category", ""),
                        "last_updated": payload.get("last_updated", ""),
                        "chunk_index": payload.get("chunk_index", 0),
                    },
                )

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        for fid, score in ranked:
            source_map[fid].score = score

        return [source_map[fid] for fid, _ in ranked]

    def format_context(self, passages: list[RetrievedPassage]) -> str:
        if not passages:
            return "No relevant documentation found."

        parts = []
        for i, p in enumerate(passages, 1):
            parts.append(f"[{i}] Score: {p.score:.3f} | Source: {p.source}\n{p.text}")
        return "\n\n---\n\n".join(parts)


def ensure_collection(
    qdrant_client: QdrantClient,
    collection_name: str,
    vector_size: int = 1536,
) -> None:
    try:
        qdrant_client.get_collection(collection_name)
        logger.info("Collection '%s' already exists", collection_name)
    except Exception:
        qdrant_client.recreate_collection(
            collection_name=collection_name,
            vectors_config={"dense": VectorParams(size=vector_size, distance=Distance.COSINE)},
            sparse_vectors_config={
                "bm25": SparseVectorParams(
                    index=models.SparseIndexParams(),
                    modifier=models.Modifier.IDF,
                )
            },
        )
        logger.info("Created collection '%s' with hybrid schema", collection_name)
