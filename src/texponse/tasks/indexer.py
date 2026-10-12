from __future__ import annotations

import asyncio
import logging
import os

from texponse.config import Settings, get_settings
from texponse.rag.retriever import ensure_collection

logger = logging.getLogger("texponse.cli")


def cli() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_index_docs(settings))


async def _index_docs(settings: Settings) -> None:
    # NOTE: no create_all() here — docs indexing only touches Qdrant.
    # Requiring Postgres (and its DNS) broke `texponse-index` on machines
    # where POSTGRES_HOST points at a docker-compose service name.

    from qdrant_client import QdrantClient

    qdrant = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    from texponse.rag.embedder import EmbeddingPipeline
    from texponse.rag.retriever import get_embedder

    embedder = get_embedder(settings.embedder_model)
    ensure_collection(qdrant, settings.qdrant_collection, vector_size=embedder.dim)

    pipeline = EmbeddingPipeline(qdrant, embedder, settings.qdrant_collection)

    docs_path = "docs/knowledge"
    if os.path.isdir(docs_path):
        for filename in sorted(os.listdir(docs_path)):
            if filename.endswith(".md"):
                filepath = os.path.join(docs_path, filename)
                with open(filepath, encoding="utf-8") as f:
                    text = f.read()
                count = await pipeline.index_document(
                    document_id=filename,
                    text=text,
                    source_url=filepath,
                    category="docs",
                )
                logger.info("Indexed %s: %d chunks", filename, count)
    else:
        logger.warning("No docs/knowledge directory found, skipping indexing")

    try:
        qdrant.close()
    except Exception as exc:
        logger.warning("Qdrant client close failed: %s", exc)
