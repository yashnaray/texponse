from __future__ import annotations

import asyncio
import logging
import os

from texponse.config import Settings, get_settings
from texponse.db.database import create_all
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
    await create_all()

    from qdrant_client import QdrantClient

    qdrant = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    ensure_collection(qdrant, settings.qdrant_collection)

    from texponse.integrations.openrouter import OpenRouterClient
    from texponse.rag.embedder import EmbeddingPipeline

    openrouter = OpenRouterClient(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
    )

    pipeline = EmbeddingPipeline(qdrant, openrouter, settings.qdrant_collection)

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

    await openrouter._client.close()
