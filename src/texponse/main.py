from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from texponse.config import get_settings
from texponse.db.database import create_all

logger = logging.getLogger("texponse.main")


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await create_all()
        from qdrant_client import QdrantClient

        from texponse.rag.retriever import ensure_collection

        qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        try:
            ensure_collection(qdrant, settings.qdrant_collection)
        finally:
            try:
                qdrant.close()
            except Exception as exc:
                logger.warning("Qdrant client close failed: %s", exc)
        yield

    app = FastAPI(
        title="Texponse API",
        description="AI-driven voice/SMS support agent",
        version="0.1.0",
        lifespan=lifespan,
    )

    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[settings.api_rate_limit],
        storage_uri=settings.redis_url,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.post("/webhooks/aircall", status_code=status.HTTP_200_OK)
    @limiter.limit(settings.webhook_rate_limit)
    async def aircall_webhook(request: Request) -> Response:
        from texponse.db.database import get_async_session
        from texponse.db.models import (
            ConversationEvent,
            ConversationEventType,
        )
        from texponse.db.repositories import ConversationRepository
        from texponse.integrations.aircall import AircallClient
        from texponse.security.webhook_validator import verify_aircall_webhook

        settings = get_settings()
        payload = await verify_aircall_webhook(request, settings)

        normalized = AircallClient.normalize_event(payload)
        event_type_raw = normalized.get("event_type", "")
        try:
            event_type = ConversationEventType(event_type_raw)
        except ValueError:
            event_type = ConversationEventType.WEBHOOK_RECEIVED

        session = get_async_session()
        repo = ConversationRepository(session)

        try:
            company_id = payload.get("company_id", "default")
            aircall_id = (
                normalized.get("aircall_call_id") or normalized.get("aircall_message_id") or ""
            )

            conv = await repo.get_or_create(
                aircall_id=aircall_id,
                company_id=company_id,
                direction=normalized.get("direction", "inbound"),
                channel="sms" if event_type_raw.startswith("message.") else "call",
            )

            event = ConversationEvent(
                conversation_id=conv.id,
                event_type=event_type,
                aircall_call_id=normalized.get("aircall_call_id"),
                aircall_message_id=normalized.get("aircall_message_id"),
                from_number=normalized.get("from_number"),
                to_number=normalized.get("to_number"),
                content=normalized.get("content"),
                direction=normalized.get("direction"),
                raw_payload=payload,
            )
            await repo.add_event(event)
        finally:
            await session.close()

        if event_type in (
            ConversationEventType.MESSAGE_RECEIVED,
            ConversationEventType.CALL_ANSWRED,
            ConversationEventType.CALL_ENDED,
        ):
            from texponse.tasks.background import generate_response_task

            generate_response_task.delay(
                company_id=company_id,
                aircall_id=aircall_id,
                event_type=event_type_raw,
                from_number=normalized.get("from_number") or "",
                to_number=normalized.get("to_number") or "",
                content=normalized.get("content"),
            )

        return JSONResponse({"status": "received"})

    @app.post("/responses/generate", status_code=status.HTTP_200_OK)
    @limiter.limit(settings.api_rate_limit)
    async def generate_response_endpoint(request: Request) -> JSONResponse:
        from texponse.tasks.background import generate_response_task

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"status": "error", "message": "Invalid JSON body"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(body, dict) or not body.get("aircall_id"):
            return JSONResponse(
                {"status": "error", "message": "aircall_id is required"},
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        generate_response_task.delay(
            company_id=body.get("company_id", "default"),
            aircall_id=body.get("aircall_id", ""),
            event_type=body.get("event_type", "message.received"),
            from_number=body.get("from_number", ""),
            to_number=body.get("to_number", ""),
            content=body.get("content"),
        )
        return JSONResponse({"status": "processing", "message": "Response queued"})

    return app


app = create_app()


def cli() -> None:
    settings = get_settings()
    uvicorn.run(
        "texponse.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        reload=settings.debug,
    )


if __name__ == "__main__":
    cli()
