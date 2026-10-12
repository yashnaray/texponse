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

logger = logging.getLogger(__name__)


@dataclass
class RetrievedPassage:
    text: str
    score: float
    source: str
    metadata: dict[str, Any]


class Embedder:
    """Local ONNX embeddings via FastEmbed (no API calls, no quota).

    Replaces the old OpenRouter-hosted `openai/text-embedding-3-small`
    (1536-dim). Default model `BAAI/bge-small-en-v1.5` is 384-dim.
    """

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        from fastembed import TextEmbedding

        self._model_name = model
        self._emb = TextEmbedding(model)

    _INSTANCES: dict[str, Any] = {}

    @property
    def dim(self) -> int:
        return int(self._emb.embedding_size)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        return await asyncio.to_thread(
            lambda: [list(map(float, v)) for v in self._emb.embed(texts)]
        )

    async def embed_query(self, text: str) -> list[float]:
        # NOTE: no BGE instruction prefix here — FastEmbed already handles the
        # model's expected query formatting, and an explicit prefix measured
        # strictly worse (dense/sparse top-10 overlap 1/10 -> 0/10).
        embeddings = await self.embed_texts([text])
        return embeddings[0]

    @staticmethod
    def build_sparse_vector(text: str) -> SparseVector:
        import hashlib
        import re as _re

        # Strip punctuation so "club." and "club" hash identically; drop
        # empties so stray punctuation doesn't create phantom tokens.
        tokens = [_re.sub(r"[^a-z0-9]", "", t) for t in text.lower().split()]
        token_set = sorted({t for t in tokens if t})
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
        min_score: float = 0.0,
    ) -> list[RetrievedPassage]:
        """Hybrid dense+sparse retrieval with RRF fusion.

        `min_score` optionally floors the fused RRF score (k=60). NOTE: the
        default is 0.0 (no floor) — measured data shows both legs return
        relevant-but-different clients on near-duplicate corpora, so a
        corroboration floor of ~0.02 drops good hits. Only raise this if
        irrelevant context (not missing context) is the observed failure.
        """
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
        relevant = [p for p in passages if p.score >= min_score]
        return relevant[: self._top_k]

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
                        "company_id": payload.get("company_id", ""),
                        "phone_hashes": payload.get("phone_hashes", []),
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
                        "company_id": payload.get("company_id", ""),
                        "phone_hashes": payload.get("phone_hashes", []),
                    },
                )

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        for fid, score in ranked:
            source_map[fid].score = score

        return [source_map[fid] for fid, _ in ranked]


def get_embedder(model: str = "BAAI/bge-small-en-v1.5") -> Embedder:
    """Process-wide singleton: avoids reloading the ONNX model per task."""
    if model not in Embedder._INSTANCES:
        Embedder._INSTANCES[model] = Embedder(model)
    return Embedder._INSTANCES[model]

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
    vector_size: int = 384,
) -> None:
    def _create() -> None:
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
        # Payload indexes required for caller-scoped filtering (category,
        # company_id, phone_hashes). Without these, Qdrant falls back to
        # slow scans and may skip the filter under load.
        for field, schema in (
            ("category", models.PayloadSchemaType.KEYWORD),
            ("company_id", models.PayloadSchemaType.KEYWORD),
            ("phone_hashes", models.PayloadSchemaType.KEYWORD),
        ):
            try:
                qdrant_client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception as exc:  # index may already exist
                logger.debug("Payload index '%s' not created: %s", field, exc)

    try:
        info = qdrant_client.get_collection(collection_name)
        try:
            vectors = info.config.params.vectors if info.config.params else None
            existing: int | None = None
            if isinstance(vectors, dict):
                vp = vectors.get("dense")
                if isinstance(vp, VectorParams):
                    existing = int(vp.size)
            elif isinstance(vectors, VectorParams):
                existing = int(vectors.size)
        except Exception:
            existing = None
        if existing is not None and int(existing) != int(vector_size):
            logger.warning(
                "Collection '%s' dim %s != %s, recreating (old points dropped)",
                collection_name,
                existing,
                vector_size,
            )
            _create()
        else:
            logger.info("Collection '%s' already exists", collection_name)
    except Exception:
        _create()
