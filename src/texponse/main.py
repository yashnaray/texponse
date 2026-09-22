from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from texponse.config import get_settings
from texponse.db.database import create_all
from texponse.tasks.background import app as celery_app
from texponse.tasks.background import generate_response_task

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

    async def _ingest(normalized: dict[str, Any], payload: dict[str, Any]) -> None:
        from texponse.db.database import get_async_session
        from texponse.db.models import (
            ConversationEvent,
            ConversationEventType,
        )
        from texponse.db.repositories import ConversationRepository

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
            generate_response_task.delay(
                company_id=company_id,
                aircall_id=aircall_id,
                event_type=event_type_raw,
                from_number=normalized.get("from_number") or "",
                to_number=normalized.get("to_number") or "",
                content=normalized.get("content"),
            )

    @app.post("/webhooks/aircall", status_code=status.HTTP_200_OK)
    @limiter.limit(settings.webhook_rate_limit)
    async def aircall_webhook(request: Request) -> Response:
        from texponse.integrations.aircall import AircallClient
        from texponse.security.webhook_validator import verify_aircall_webhook

        webhook_settings = get_settings()
        payload = await verify_aircall_webhook(request, webhook_settings)

        normalized = AircallClient.normalize_event(payload)
        await _ingest(normalized, payload)

        return JSONResponse({"status": "received"})

    @app.post("/webhooks/aircall/callback", status_code=status.HTTP_200_OK)
    @limiter.limit(settings.webhook_rate_limit)
    async def aircall_callback(request: Request) -> Response:
        """Receiver for proxy-mode messaging callbacks (callbackUrl).

        Proxy callbacks use ``{"event_name": ..., "data": {...}}`` and are
        not signed with the webhook secret, so they are handled separately
        from signed webhook events. If ``AIRCALL_CALLBACK_TOKEN`` is set, the
        caller must present it as ``?token=`` or the
        ``X-Aircall-Callback-Token`` header.
        """
        import json as jsonlib

        from texponse.integrations.aircall import AircallClient

        callback_settings = get_settings()
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"status": "error", "message": "Invalid JSON body"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"status": "error", "message": "Invalid JSON payload"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        expected_token = callback_settings.aircall_callback_token
        if expected_token:
            provided = (
                request.query_params.get("token", "")
                or request.headers.get("X-Aircall-Callback-Token", "")
                or str(payload.get("token", "") or "")
            )
            if provided != expected_token:
                return JSONResponse(
                    {"status": "error", "message": "Invalid callback token"},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
        else:
            logger.warning("Accepting unsigned Aircall callback (AIRCALL_CALLBACK_TOKEN unset)")

        logger.info("Aircall callback payload: %s", jsonlib.dumps(payload)[:2000])
        normalized = AircallClient.normalize_callback_event(payload)
        await _ingest(normalized, payload)

        return JSONResponse({"status": "received"})

    @app.post("/responses/generate", status_code=status.HTTP_200_OK)
    @limiter.limit(settings.api_rate_limit)
    async def generate_response_endpoint(request: Request) -> JSONResponse:
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
        task = generate_response_task.delay(
            company_id=body.get("company_id", "default"),
            aircall_id=body.get("aircall_id", ""),
            event_type=body.get("event_type", "message.received"),
            from_number=body.get("from_number", ""),
            to_number=body.get("to_number", ""),
            content=body.get("content"),
        )
        try:
            outcome = await asyncio.to_thread(task.get, timeout=100.0)
        except CeleryTimeoutError:
            return JSONResponse(
                {
                    "status": "processing",
                    "message": "Response still processing",
                    "task_id": task.id,
                },
                status_code=status.HTTP_202_ACCEPTED,
            )
        except Exception as exc:
            logger.exception("Response generation failed for task %s", task.id)
            return JSONResponse(
                {"status": "failed", "message": str(exc), "task_id": task.id},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        if isinstance(outcome, dict) and outcome.get("delivery_error"):
            return JSONResponse(
                {
                    "status": "generated_with_delivery_error",
                    "task_id": task.id,
                    "result": outcome,
                },
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
        return JSONResponse(
            {"status": "completed", "task_id": task.id, "result": outcome},
            status_code=status.HTTP_200_OK,
        )

    @app.get("/responses/result/{task_id}", status_code=status.HTTP_200_OK)
    @limiter.limit(settings.api_rate_limit)
    async def generate_response_result(request: Request, task_id: str) -> JSONResponse:
        _ = request
        async_result = celery_app.AsyncResult(task_id)
        state = async_result.state
        if state in ("PENDING", "STARTED", "RETRY"):
            return JSONResponse(
                {"status": "processing", "task_id": task_id, "state": state},
                status_code=status.HTTP_202_ACCEPTED,
            )
        if state == "FAILURE":
            return JSONResponse(
                {
                    "status": "failed",
                    "task_id": task_id,
                    "message": str(async_result.result),
                },
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return JSONResponse(
            {"status": "completed", "task_id": task_id, "result": async_result.result},
            status_code=status.HTTP_200_OK,
        )

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
