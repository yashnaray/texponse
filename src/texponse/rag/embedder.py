from __future__ import annotations

import logging
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http.models import PointStruct, SparseVector

from texponse.rag.retriever import Embedder

logger = logging.getLogger(__name__)


class EmbeddingPipeline:
    def __init__(
        self,
        qdrant: QdrantClient,
        embedder: Embedder,
        collection_name: str,
    ) -> None:
        self._qdrant = qdrant
        self._embedder = embedder
        self._collection = collection_name
        self._chunk_size = 512
        self._chunk_overlap = 50

    def _build_sparse_vector(self, text: str) -> SparseVector:
        return Embedder.build_sparse_vector(text)

    async def index_document(
        self,
        document_id: str,
        text: str,
        source_url: str,
        category: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        chunks = self._chunk_text(text)
        if not chunks:
            logger.warning("Document '%s' produced no chunks", document_id)
            return 0

        embeddings = await self._embedder.embed_texts(chunks)

        import asyncio
        import uuid

        points: list[PointStruct] = []
        for i, (chunk, vector) in enumerate(zip(chunks, embeddings, strict=True)):
            point_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{i}").hex
            sparse = self._build_sparse_vector(chunk)
            points.append(
                PointStruct(
                    id=point_id,
                    vector={
                        "dense": vector,
                        "bm25": sparse,
                    },
                    payload={
                        "text": chunk,
                        "source_url": source_url,
                        "category": category,
                        "company_id": (
                            metadata.get("company_id", "global") if metadata else "global"
                        ),
                        "document_id": document_id,
                        "chunk_index": i,
                        "last_updated": metadata.get("last_updated", "") if metadata else "",
                        **(metadata.get("extra", {}) if metadata else {}),
                    },
                )
            )

        await asyncio.to_thread(
            self._qdrant.upsert,
            collection_name=self._collection,
            points=points,
            wait=True,
        )
        logger.info("Indexed %d chunks for document '%s' in Qdrant", len(points), document_id)
        return len(points)

    def _chunk_text(self, text: str) -> list[str]:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        words = text.split()
        if not words:
            return []

        chunks: list[str] = []
        chunk_words: list[str] = []

        for word in words:
            chunk_words.append(word)
            if len(chunk_words) >= self._chunk_size:
                chunks.append(" ".join(chunk_words))
                overlap = chunk_words[-self._chunk_overlap :] if self._chunk_overlap > 0 else []
                chunk_words = overlap

        if chunk_words:
            chunks.append(" ".join(chunk_words))

        return [c.strip() for c in chunks if c.strip()]

    async def sync_from_jobber(self, access_token: str) -> int:
        return 0
