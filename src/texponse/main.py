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

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
except ImportError:  # pragma: no cover - metrics optional without dep
    CONTENT_TYPE_LATEST = "text/plain"
    Counter = Histogram = None  # type: ignore[assignment,misc]

    def generate_latest() -> bytes:  # type: ignore[misc]
        return b"# prometheus-client not installed\n"


REQUEST_COUNT = (
    Counter("texponse_http_requests_total", "Total HTTP requests", ["method", "path", "status"])
    if Counter is not None
    else None
)
REQUEST_LATENCY = (
    Histogram("texponse_http_request_duration_seconds", "HTTP request latency", ["method", "path"])
    if Histogram is not None
    else None
)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await create_all()
        from qdrant_client import QdrantClient

        from texponse.rag.retriever import ensure_collection

        qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        try:
            from texponse.rag.retriever import Embedder as _LifEmbedder

            ensure_collection(
                qdrant,
                settings.qdrant_collection,
                vector_size=_LifEmbedder(settings.embedder_model).dim,
            )
        finally:
            try:
                qdrant.close()
            except Exception as exc:
                logger.warning("Qdrant client close failed: %s", exc)
        # Seed Jobber -> Qdrant on startup so beat's hourly timer doesn't
        # leave the first hour empty. Fire-and-forget to the worker.
        try:
            from texponse.tasks.background import (
                refresh_jobber_tokens_task,
                sync_jobber_data_task,
            )

            refresh_jobber_tokens_task.delay()
            sync_jobber_data_task.delay()
            logger.info("Queued startup Jobber token refresh + data sync")
        except Exception as exc:
            logger.warning("Startup Jobber sync queue failed: %s", exc)
        yield

    app = FastAPI(
        title="Texponse API",
        description="AI-driven voice/SMS support agent",
        version="0.1.0",
        lifespan=lifespan,
    )

    storage_uri = settings.redis_url
    try:
        import redis as sync_redis

        probe = sync_redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        probe.ping()
        probe.close()
    except Exception as exc:
        logger.warning("Redis unreachable, rate limiting falls back to memory: %s", exc)
        storage_uri = "memory://"
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[settings.api_rate_limit],
        storage_uri=storage_uri,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:
        logger.warning("OTel FastAPI instrumentation skipped: %s", exc)

    @app.middleware("http")
    async def _prometheus_middleware(request: Request, call_next: Any) -> Any:
        import time

        start = time.perf_counter()
        response = await call_next(request)
        if REQUEST_COUNT is not None and REQUEST_LATENCY is not None:
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path) if route else request.url.path
            REQUEST_COUNT.labels(request.method, path, str(response.status_code)).inc()
            REQUEST_LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
        return response

    @app.get("/metrics")
    @limiter.exempt
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    @limiter.exempt
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        import httpx
        import sqlalchemy

        from texponse.db.database import get_async_engine

        engine = get_async_engine()

        checks: dict[str, str] = {}
        ok = True
        try:
            async with engine.connect() as conn:
                await conn.execute(sqlalchemy.text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:
            ok = False
            checks["postgres"] = f"error: {exc}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{settings.qdrant_url}/collections")
                r.raise_for_status()
            checks["qdrant"] = "ok"
        except Exception as exc:
            ok = False
            checks["qdrant"] = f"error: {exc}"
        try:
            import redis.asyncio as aioredis

            rc = aioredis.from_url(settings.redis_url, socket_connect_timeout=5)
            await rc.ping()
            await rc.aclose()
            checks["redis"] = "ok"
        except Exception as exc:
            ok = False
            checks["redis"] = f"error: {exc}"
        try:
            insp = celery_app.control.inspect(timeout=5)
            stats = insp.stats() if insp else None
            checks["celery"] = "ok" if stats else "no workers responding"
            if not stats:
                ok = False
        except Exception as exc:
            ok = False
            checks["celery"] = f"error: {exc}"
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/health/prod-check")
    async def prod_check() -> JSONResponse:
        issues = get_settings().validate_production()
        code = status.HTTP_200_OK if not issues else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse({"ready_for_prod": not issues, "issues": issues}, status_code=code)

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
            company_id = payload.get("company_id", "servicePros")
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
            company_id=body.get("company_id", "servicePros"),
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

    @app.get("/auth/jobber/login", status_code=status.HTTP_200_OK)
    async def jobber_login(request: Request) -> JSONResponse:
        import secrets
        from urllib.parse import urlencode

        js = get_settings()
        state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": js.jobber_client_id,
            "redirect_uri": js.jobber_redirect_uri,
            "state": state,
        }
        url = f"https://api.getjobber.com/api/oauth/authorize?{urlencode(params)}"
        return JSONResponse({"authorize_url": url, "state": state})

    @app.get("/webhooks/jobber/callback", status_code=status.HTTP_200_OK)
    async def jobber_callback(request: Request) -> JSONResponse:
        from datetime import datetime, timedelta

        code = request.query_params.get("code", "")
        if not code:
            return JSONResponse(
                {"status": "error", "message": "missing ?code=... (Jobber denied or bad redirect)"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        company_id = request.query_params.get("company_id", "servicePros")
        js = get_settings()
        from texponse.integrations.jobber import JobberClient

        client = JobberClient(
            client_id=js.jobber_client_id,
            client_secret=js.jobber_client_secret,
            redirect_uri=js.jobber_redirect_uri,
            graphql_version=js.jobber_graphql_version,
            base_url=js.jobber_base_url,
        )
        try:
            tokens = await client.exchange_code(code)
        except Exception as exc:
            logger.exception("Jobber code exchange failed")
            return JSONResponse(
                {"status": "error", "message": f"code exchange failed: {exc}"},
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                await client.close()
        if "access_token" not in tokens:
            return JSONResponse(
                {"status": "error", "message": f"no access_token: {tokens}"},
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
        from texponse.db.database import get_async_session
        from texponse.db.models import JobberToken
        from texponse.db.repositories import JobberTokenRepository

        session = get_async_session()
        try:
            repo = JobberTokenRepository(session)
            expires_in = int(tokens.get("expires_in", 3600))
            existing = await repo.get(company_id)
            if existing:
                existing.access_token = tokens["access_token"]
                existing.refresh_token = tokens.get("refresh_token", existing.refresh_token)
                existing.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
                await session.commit()
            else:
                await repo.save(
                    JobberToken(
                        company_id=company_id,
                        access_token=tokens["access_token"],
                        refresh_token=tokens.get("refresh_token", ""),
                        expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
                    )
                )
                await session.commit()
        finally:
            await session.close()
        # quick sanity check: query Jobber
        return JSONResponse({"status": "connected", "company_id": company_id})

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
