from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from celery import Celery
from qdrant_client import QdrantClient
from sqlmodel.ext.asyncio.session import AsyncSession

from texponse.config import get_settings
from texponse.db.database import get_async_session
from texponse.db.repositories import ConversationRepository
from texponse.integrations.aircall import AircallClient
from texponse.integrations.jobber import JobberClient
from texponse.integrations.openrouter import OpenRouterClient
from texponse.rag.orchestrator import LLMOrchestrator
from texponse.rag.prompt_builder import PromptBuilder, PromptContext
from texponse.rag.retriever import Embedder, HybridRetriever

logger = logging.getLogger(__name__)


def _create_celery() -> Celery:
    settings = get_settings()
    app = Celery(
        "texponse",
        broker=settings.celery_broker,
        backend=settings.celery_backend,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_time_limit=120,
        task_soft_time_limit=100,
        broker_connection_retry_on_startup=True,
        broker_connection_retry=True,
        beat_schedule={
            "refresh-jobber-tokens": {
                "task": "texponse.tasks.refresh_jobber_tokens",
                "schedule": 900.0,  # 15 minutes
            },
            "sync-jobber-data": {
                "task": "texponse.tasks.sync_jobber_data",
                "schedule": 1800.0,  # 30 minutes
            },
            "health-check": {
                "task": "texponse.tasks.health_check",
                "schedule": 60.0,  # 1 minute
            },
        },
    )
    return app


app = _create_celery()


@app.task(bind=True, name="texponse.tasks.generate_response")
def generate_response_task(
    self: Any,
    company_id: str,
    aircall_id: str,
    event_type: str,
    from_number: str,
    to_number: str,
    content: str | None,
) -> dict[str, Any]:
    return asyncio.run(
        _process_conversation(
            company_id=company_id,
            aircall_id=aircall_id,
            event_type=event_type,
            from_number=from_number,
            to_number=to_number,
            content=content,
        )
    )


async def _process_conversation(
    company_id: str,
    aircall_id: str,
    event_type: str,
    from_number: str,
    to_number: str,
    content: str | None,
) -> dict[str, Any]:
    settings = get_settings()

    session = get_async_session()
    repo = ConversationRepository(session)

    aircall = AircallClient(settings)
    openrouter = OpenRouterClient(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        default_model=settings.openrouter_default_model,
        fallback_model=settings.openrouter_fallback_model,
        timeout=settings.openrouter_timeout,
    )
    prompt_builder = PromptBuilder(settings, openrouter)
    orchestrator = LLMOrchestrator(settings, openrouter, prompt_builder)

    qdrant = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    embedder_instance = Embedder(openrouter)
    retriever = HybridRetriever(
        qdrant, embedder_instance, settings.qdrant_collection, settings.rag_top_k
    )

    conv = await repo.get_or_create(
        aircall_id=aircall_id,
        company_id=company_id,
        direction="inbound" if event_type == "message.received" else "inbound",
        channel="sms" if event_type.startswith("message.") else "call",
    )

    history_records = await repo.get_history(conv.id)
    history_msgs = [
        {"role": "user" if e.direction == "inbound" else "assistant", "content": e.content or ""}
        for e in history_records
        if e.content
    ]

    query = content or ""

    passages = await retriever.retrieve(query=query)

    jobber_context: dict[str, Any] | None = None
    jobber_token = await _get_jobber_token(session, company_id)
    if jobber_token and from_number:
        try:
            jobber_client = JobberClient(
                client_id=settings.jobber_client_id,
                client_secret=settings.jobber_client_secret,
                redirect_uri=settings.jobber_redirect_uri,
                graphql_version=settings.jobber_graphql_version,
                base_url=settings.jobber_base_url,
            )
            client_data = await jobber_client.find_client_by_phone(
                from_number, jobber_token.access_token
            )
            if client_data:
                jobber_context = client_data
        except Exception as exc:
            logger.warning("Jobber enrichment failed: %s", exc)

    prompt_context = PromptContext(
        passages=passages,
        conversation_history=history_msgs,
        jobber_context=jobber_context,
        customer_name=jobber_context.get("name") if jobber_context else None,
        customer_phone=from_number,
    )

    response = await orchestrator.generate(
        query=query,
        context=prompt_context,
        company_name="Texponse",
    )

    response_model = orchestrator.to_db_model(response, conv.id)
    await repo.save_response(response_model)

    await aircall.send_message(
        from_number=to_number,
        to_number=from_number,
        content=response.content[:1500],
    )

    await aircall.close()
    await openrouter._client.close()
    await session.close()

    return {
        "conversation_id": conv.id,
        "response_preview": response.content[:200],
        "model_used": response.model_used,
        "tokens": {
            "prompt": response.prompt_tokens,
            "completion": response.completion_tokens,
            "total": response.total_tokens,
        },
        "latency_ms": response.latency_ms,
        "was_fallback": response.was_fallback,
        "passages_retrieved": response.retrieved_passage_count,
    }


async def _get_jobber_token(session: AsyncSession, company_id: str) -> Any:
    from texponse.db.repositories import JobberTokenRepository

    token_repo = JobberTokenRepository(session)
    return await token_repo.get(company_id)


@app.task(name="texponse.tasks.refresh_jobber_tokens")
def refresh_jobber_tokens_task() -> dict[str, Any]:
    return asyncio.run(_refresh_jobber_tokens())


async def _refresh_jobber_tokens() -> dict[str, Any]:
    settings = get_settings()
    session = get_async_session()
    from texponse.db.repositories import JobberTokenRepository

    token_repo = JobberTokenRepository(session)
    jobber = JobberClient(
        client_id=settings.jobber_client_id,
        client_secret=settings.jobber_client_secret,
        redirect_uri=settings.jobber_redirect_uri,
        graphql_version=settings.jobber_graphql_version,
        base_url=settings.jobber_base_url,
    )

    refreshed = 0
    try:
        async with session:
            from sqlalchemy import text as sql_text

            stmt = sql_text("SELECT * FROM jobber_tokens WHERE expires_at < :now")
            result = await session.exec(
                stmt,
                {"now": datetime.utcnow()},
            )  # type: ignore[call-overload]
            tokens = result.all()
            for token in tokens:
                if await token_repo.is_expired(token):
                    resp = await jobber.refresh_token(token.refresh_token)
                    token.access_token = resp["access_token"]
                    token.refresh_token = resp.get("refresh_token", token.refresh_token)
                    token.expires_at = datetime.utcnow() + timedelta(seconds=resp["expires_in"])
                    await session.commit()
                    refreshed += 1
    finally:
        await session.close()
        await jobber.close()

    return {"refreshed_tokens": refreshed}


@app.task(name="texponse.tasks.sync_jobber_data")
def sync_jobber_data_task() -> dict[str, Any]:
    return asyncio.run(_sync_jobber_data())


async def _sync_jobber_data() -> dict[str, Any]:
    logger.info("Jobber data sync triggered (placeholder)")
    return {"synced": True}


@app.task(name="texponse.tasks.health_check")
def health_check_task() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "redis_url": settings.redis_url,
        "qdrant_url": settings.qdrant_url,
    }
