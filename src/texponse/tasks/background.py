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
from texponse.rag.orchestrator import LLMOrchestrator
from texponse.rag.prompt_builder import PromptBuilder, PromptContext
from texponse.rag.retriever import Embedder, HybridRetriever, RetrievedPassage

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
                "schedule": 3600.0,  # 1 hour
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
    from texponse.integrations.openrouter import build_llm_client

    openrouter = build_llm_client(settings)
    prompt_builder = PromptBuilder(settings, openrouter)
    orchestrator = LLMOrchestrator(settings, openrouter, prompt_builder)

    qdrant = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )
    embedder_instance = Embedder(settings.embedder_model)
    retriever = HybridRetriever(
        qdrant, embedder_instance, settings.qdrant_collection, settings.rag_top_k
    )

    conv = await repo.get_or_create(
        aircall_id=aircall_id,
        company_id=company_id,
        direction="inbound",
        channel="sms" if event_type.startswith("message.") else "call",
    )

    if content:
        from texponse.db.models import ConversationEvent, ConversationEventType

        try:
            inbound_type = ConversationEventType(event_type)
        except ValueError:
            inbound_type = ConversationEventType.WEBHOOK_RECEIVED
        await repo.add_event(
            ConversationEvent(
                conversation_id=conv.id,
                event_type=inbound_type,
                from_number=from_number or None,
                to_number=to_number or None,
                content=content,
                direction="inbound",
                raw_payload={"event_type": event_type},
            )
        )

    history_records = await repo.get_history(conv.id)
    history_msgs = [
        {"role": "user" if e.direction == "inbound" else "assistant", "content": e.content or ""}
        for e in history_records
        if e.content
    ]

    query = (content or "").strip()

    from qdrant_client.http import models as _qmodels

    from texponse.security.guardrails import active_peppers
    from texponse.security.guardrails import phone_hash as _caller_hash
    from texponse.security.guardrails import redact_pii as _redact

    _peppers = active_peppers(
        settings.secret_key, settings.phone_hash_pepper, settings.phone_hash_pepper_previous
    )
    # MIGRATION: points indexed before peppering used pepper="". Include the
    # legacy hash in the query until a Jobber re-sync rewrites all points,
    # otherwise Qdrant returns 0 hits (what you're seeing in the log).
    _all_peppers = list(dict.fromkeys(_peppers + [""]))
    _hashes = sorted({_caller_hash(from_number, pepper=p) for p in _all_peppers} - {""})
    _ch = _hashes[0] if _hashes else ""
    # Dev god mode: hard-gated to development. No filters, no verification.
    _god_mode = bool(settings.dev_disable_security and settings.app_env == "development")
    if _god_mode:
        logger.warning(
            "DEV GOD MODE: all chunk access security disabled. "
            "Never enable outside development."
        )
    # Dev-only escape hatch: gated to development env, company isolation stays on.
    _dev_bypass = bool(settings.dev_bypass_chunk_security and settings.app_env == "development")
    # Dispatcher allowlist: these numbers may look up any client in their OWN
    # company (e.g. office staff texting from a known line). Company isolation
    # stays enforced; the caller phone-hash check is skipped for them.
    _allowlisted = False
    if settings.chunk_security_bypass_numbers and from_number:
        _digits = "".join(c for c in from_number if c.isdigit())[-10:]
        _allowed = {
            "".join(c for c in n if c.isdigit())[-10:]
            for n in settings.chunk_security_bypass_numbers.split(",")
        } - {""}
        _allowlisted = _digits in _allowed
    _bypass = _dev_bypass or _allowlisted
    if _allowlisted:
        logger.info(
            "Dispatcher bypass: %s scoped to company=%s (caller check skipped).",
            _digits,
            company_id,
        )
    if _dev_bypass:
        logger.warning(
            "DEV BYPASS active: caller phone-hash check skipped (company=%s). "
            "Never enable outside development.",
            company_id,
        )
    if _god_mode:
        _qfilter = None
    elif _bypass:
        _qfilter = _qmodels.Filter(
            should=[
                _qmodels.Filter(
                    must=[
                        _qmodels.FieldCondition(
                            key="category",
                            match=_qmodels.MatchValue(value="jobber"),
                        ),
                        _qmodels.FieldCondition(
                            key="company_id",
                            match=_qmodels.MatchValue(value=company_id),
                        ),
                    ]
                ),
                _qmodels.Filter(
                    must_not=[
                        _qmodels.FieldCondition(
                            key="category",
                            match=_qmodels.MatchValue(value="jobber"),
                        ),
                    ]
                ),
            ]
        )
    elif _ch:
        _qfilter = _qmodels.Filter(
            should=[
                _qmodels.Filter(
                    must=[
                        _qmodels.FieldCondition(
                            key="category",
                            match=_qmodels.MatchValue(value="jobber"),
                        ),
                        _qmodels.FieldCondition(
                            key="company_id",
                            match=_qmodels.MatchValue(value=company_id),
                        ),
                        _qmodels.FieldCondition(
                            key="phone_hashes",
                            match=_qmodels.MatchAny(any=_hashes),
                        ),
                    ]
                ),
                _qmodels.Filter(
                    must_not=[
                        _qmodels.FieldCondition(
                            key="category",
                            match=_qmodels.MatchValue(value="jobber"),
                        ),
                    ]
                ),
            ]
        )
    else:
        # Unknown caller: never return jobber client chunks.
        _qfilter = _qmodels.Filter(
            must_not=[
                _qmodels.FieldCondition(
                    key="category",
                    match=_qmodels.MatchValue(value="jobber"),
                ),
            ]
        )
    passages = []
    if query:
        try:
            passages = await retriever.retrieve(query=query, filters=_qfilter)
        except Exception as exc:
            logger.exception("Qdrant retrieve failed (collection=%s): %s", settings.qdrant_collection, exc)
        logger.info(
            "Qdrant retrieve: %d passages for company=%s (caller hash %s)",
            len(passages),
            company_id,
            "present" if _ch else "missing",
        )
    # Defense-in-depth: Qdrant filter is the primary guard, but never trust it
    # alone — drop any jobber passage that isn't scoped to this caller/company.
    # Skipped entirely in dev god mode.
    if passages and not _god_mode:
        verified: list[RetrievedPassage] = []
        for p in passages:
            meta = p.metadata or {}
            if meta.get("category") == "jobber":
                phs = set(meta.get("phone_hashes") or [])
                if not _bypass and (not _hashes or phs.isdisjoint(_hashes)):
                    logger.warning("Dropping cross-caller jobber passage from %s", p.source)
                    continue
                if meta.get("company_id", company_id) != company_id:
                    logger.warning("Dropping cross-tenant jobber passage from %s", p.source)
                    continue
            verified.append(p)
        passages = verified

    jobber_context: dict[str, Any] | None = None
    jobber_token = await _get_jobber_token(session, company_id)
    jobber_client: JobberClient | None = None
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
        finally:
            if jobber_client is not None:
                try:
                    await jobber_client.close()
                except Exception as exc:
                    logger.warning("Jobber client close failed: %s", exc)

    result_fallback_note: str | None = None

    prompt_context = PromptContext(
        passages=passages,
        conversation_history=history_msgs,
        jobber_context=jobber_context,
        customer_name=jobber_context.get("name") if jobber_context else None,
        customer_phone=from_number,
    )

    response_content: str
    model_used = "static-fallback"
    prompt_tokens = completion_tokens = total_tokens = 0
    latency_ms = 0
    was_fallback = False
    passages_count = len(passages)
    if await _llm_budget_exhausted(settings):
        # Daily quota already spent: don't even knock on the provider's door.
        logger.warning("LLM daily budget spent, serving static SMS without API call")
        response_content = (
            "Thanks for your message — we've received it and our team will follow up shortly."
        )
        model_used = "static-fallback"
        was_fallback = True
        result_fallback_note = "llm_error: daily budget exhausted"
    else:
        try:
            await _llm_spend_tick(settings)
            response = await orchestrator.generate(
                query=query,
                context=prompt_context,
                company_name="Texponse",
            )
            response_model = orchestrator.to_db_model(response, conv.id)
            await repo.save_response(response_model)
            response_content = response.content
            model_used = response.model_used
            prompt_tokens = response.prompt_tokens
            completion_tokens = response.completion_tokens
            total_tokens = response.total_tokens
            latency_ms = response.latency_ms
            was_fallback = response.was_fallback
            passages_count = response.retrieved_passage_count
        except Exception as exc:
            # LLM/RAG failure must not leave the customer with silence:
            # fall back to a static SMS reply (regular-SMS behaviour).
            logger.exception("LLM generation failed, using static fallback SMS: %s", exc)
            response_content = (
                "Thanks for your message — we've received it and our team will follow up shortly."
            )
            model_used = "static-fallback"
            was_fallback = True
            result_fallback_note = f"llm_error: {exc}"

    logger.info(
        "Generated response for conversation %s via %s: %s",
        conv.id,
        model_used,
        _redact(response_content[:200]),
    )

    result: dict[str, Any] = {
        "conversation_id": conv.id,
        "response_preview": _redact(response_content[:200]),
        "model_used": model_used,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
        },
        "latency_ms": latency_ms,
        "was_fallback": was_fallback,
        "passages_retrieved": passages_count,
    }
    if result_fallback_note is not None:
        result["fallback_note"] = result_fallback_note

    if from_number and settings.aircall_line_id is not None:
        try:
            await aircall.send_message(
                line_id=settings.aircall_line_id,
                to_number=from_number,
                content=response_content[:1500],
            )
        except Exception as exc:
            import contextlib

            detail = str(exc)
            http_response = getattr(exc, "response", None)
            if http_response is not None:
                with contextlib.suppress(Exception):
                    detail = f"{exc} | body: {http_response.text[:500]}"
            logger.warning("Aircall send_message failed: %s", detail)
            result["delivery_error"] = detail
    elif not from_number:
        logger.warning("Skipping Aircall send: missing from_number")
        result["delivery_error"] = "missing phone numbers"
    else:
        logger.warning("Skipping Aircall send: AIRCALL_LINE_ID is not configured")
        result["delivery_error"] = (
            "AIRCALL_LINE_ID is not configured; set it to the Aircall number ID "
            "configured for Public API messaging"
        )

    try:
        await aircall.close()
    except Exception as exc:
        logger.warning("Aircall client close failed: %s", exc)
    try:
        await openrouter.close()
    except Exception as exc:
        logger.warning("OpenRouter client close failed: %s", exc)
    try:
        qdrant.close()
    except Exception as exc:
        logger.warning("Qdrant client close failed: %s", exc)
    try:
        await session.close()
    except Exception as exc:
        logger.warning("DB session close failed: %s", exc)

    return result


async def _llm_budget_key(settings: Any) -> str:
    from datetime import datetime as _dt

    return f"texponse:llm_spend:{_dt.utcnow().strftime('%Y-%m-%d')}"


async def _llm_budget_exhausted(settings: Any) -> bool:
    """True when today's billable LLM attempts already hit llm_daily_budget.

    Fail-open: if Redis is unreachable we proceed (better a 429 than silence).
    """
    budget = int(getattr(settings, "effective_daily_budget", None) or getattr(settings, "llm_daily_budget", 40) or 0)
    if budget <= 0:
        return False
    try:
        import redis.asyncio as _aioredis

        rc = _aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        try:
            spent = int(await rc.get(await _llm_budget_key(settings)) or 0)
        finally:
            await rc.aclose()
        return spent >= budget
    except Exception as exc:
        logger.warning("LLM budget check failed, proceeding anyway: %s", exc)
        return False


async def _llm_spend_tick(settings: Any) -> None:
    """Count one billable LLM attempt against today's budget (24h expiry)."""
    try:
        import redis.asyncio as _aioredis

        rc = _aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        try:
            key = await _llm_budget_key(settings)
            await rc.incr(key)
            await rc.expire(key, 24 * 3600)
        finally:
            await rc.aclose()
    except Exception as exc:
        logger.warning("LLM spend tick failed (non-fatal): %s", exc)


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
        from sqlmodel import select

        from texponse.db.models import JobberToken

        stmt = select(JobberToken).where(JobberToken.expires_at < datetime.utcnow())
        result = await session.exec(stmt)
        tokens = list(result.all())
        for token in tokens:
            if await token_repo.is_expired(token):
                resp = await jobber.refresh_token(token.refresh_token)
                if "access_token" not in resp:
                    logger.warning("Jobber refresh missing access_token, skipping")
                    continue
                token.access_token = resp["access_token"]
                token.refresh_token = resp.get("refresh_token", token.refresh_token)
                expires_in = resp.get("expires_in", 3600)
                token.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
                await session.commit()
                refreshed += 1
    finally:
        await session.close()
        await jobber.close()

    return {"refreshed_tokens": refreshed}


@app.task(name="texponse.tasks.sync_jobber_data", time_limit=600, soft_time_limit=540)
def sync_jobber_data_task() -> dict[str, Any]:
    return asyncio.run(_sync_jobber_data())


async def _sync_jobber_data() -> dict[str, Any]:
    from sqlmodel import select

    from texponse.db.models import JobberToken
    from texponse.integrations.jobber import JobberClient
    from texponse.rag.retriever import ensure_collection

    settings = get_settings()
    session = get_async_session()
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    jobber = JobberClient(
        client_id=settings.jobber_client_id,
        client_secret=settings.jobber_client_secret,
        redirect_uri=settings.jobber_redirect_uri,
        graphql_version=settings.jobber_graphql_version,
        base_url=settings.jobber_base_url,
    )
    try:
        from texponse.rag.retriever import Embedder as _Embedder

        _embedder = _Embedder(settings.embedder_model)
        ensure_collection(qdrant, settings.qdrant_collection, vector_size=_embedder.dim)
        # Dev shortcut: skip the expensive Jobber re-sync when Qdrant already
        # holds sufficient client data. Hard-gated to development; prod always syncs.
        _min_chunks = settings.dev_skip_sync_min_chunks
        if settings.app_env == "development" and _min_chunks > 0:
            try:
                import asyncio as _count_asyncio

                _count = await _count_asyncio.to_thread(
                    qdrant.count,
                    collection_name=settings.qdrant_collection,
                )
                _existing = _count.count if _count is not None else 0
            except Exception as exc:
                logger.warning("Qdrant count check failed, syncing anyway: %s", exc)
                _existing = 0
            if _existing >= _min_chunks:
                logger.info(
                    "Skipping Jobber sync in dev: %d points already in %s (>= %d)",
                    _existing,
                    settings.qdrant_collection,
                    _min_chunks,
                )
                return {"skipped": True, "existing_chunks": _existing}
        result = await session.exec(select(JobberToken))
        tokens = list(result.all())
        clients_total = chunks_total = 0
        import asyncio as _asyncio
        import uuid as _uuid

        from qdrant_client.http.models import PointStruct as _Point

        sem = _asyncio.Semaphore(4)

        async def _embed_batch(texts: list[str]) -> list[list[float]]:
            async with sem:
                return await _embedder.embed_texts(texts)

        for token in tokens:
            # Refresh expired tokens inline — the beat refresh may not have run yet,
            # and without this every sync after expiry 401s with 0 chunks.
            from texponse.db.repositories import JobberTokenRepository as _TokenRepo

            _repo = _TokenRepo(session)
            if await _repo.is_expired(token):
                try:
                    resp = await jobber.refresh_token(token.refresh_token)
                except Exception as exc:
                    logger.warning("Jobber refresh failed for %s: %s", token.company_id, exc)
                    continue
                if "access_token" not in resp:
                    logger.warning("Jobber refresh missing access_token for %s", token.company_id)
                    continue
                token.access_token = resp["access_token"]
                token.refresh_token = resp.get("refresh_token", token.refresh_token)
                from datetime import datetime as _dt
                from datetime import timedelta as _td

                token.expires_at = _dt.utcnow() + _td(seconds=resp.get("expires_in", 3600))
                await session.commit()
                logger.info("Refreshed Jobber token for %s", token.company_id)
            try:
                clients = await jobber.list_all_clients(token.access_token)
            except Exception as exc:
                logger.warning("Jobber sync failed for %s: %s", token.company_id, exc)
                continue
            from texponse.security.guardrails import active_peppers
            from texponse.security.guardrails import phone_hash as _ph
            from texponse.security.guardrails import redact_pii as _redact

            _sync_pepper = active_peppers(
                settings.secret_key,
                settings.phone_hash_pepper,
                settings.phone_hash_pepper_previous,
            )[0]

            docs: list[tuple[str, str, str, dict]] = []
            for c in clients:
                phones_list = [
                    p.get("number", "") for p in (c.get("phones") or []) if p.get("number")
                ]
                hashes = sorted({_ph(p, pepper=_sync_pepper) for p in phones_list} - {""})
                jobs = (c.get("jobs") or {}).get("nodes", []) or []
                jobs_txt = "; ".join(
                    f"{j.get('title', '')} ({j.get('jobStatus', '')}, ${j.get('total', 0)}, "
                    f"{j.get('startAt', '?')}->{j.get('endAt', '?')})"
                    for j in jobs
                )
                visits = [
                    v
                    for j in jobs
                    for v in ((j.get("visits") or {}).get("nodes", []) or [])
                ]
                visits_txt = "; ".join(
                    f"{v.get('title', '')} ({v.get('visitStatus', '')}, "
                    f"{v.get('startAt', '?')}->{v.get('endAt', '?')})"
                    for v in visits
                )
                text = _redact(
                    f"Client: {c.get('name', '')}\nCompany: {c.get('companyName', '')}\n"
                    f"Balance: ${c.get('balance', 0)}\nJobs: {jobs_txt}\n"
                    f"Upcoming visits: {visits_txt or 'none listed'}"
                )
                docs.append(
                    (
                        f"jobber-{token.company_id}-{c.get('id', '')}",
                        text,
                        f"jobber://{token.company_id}/clients/{c.get('id', '')}",
                        {
                            "company_id": token.company_id,
                            "phone_hashes": hashes,
                            "phone_hash": hashes[0] if hashes else "",
                        },
                    )
                )
            # Batch embed 32 docs per API call instead of 1 call per doc.
            for i in range(0, len(docs), 32):
                batch = docs[i : i + 32]
                vectors = await _embed_batch([t for _, t, _, _ in batch])
                points = []
                for (doc_id, chunk, url, extra), vec in zip(batch, vectors, strict=True):
                    points.append(
                        _Point(
                            id=_uuid.uuid5(_uuid.NAMESPACE_URL, f"{doc_id}:0").hex,
                            vector={
                                "dense": vec,
                                "bm25": _Embedder.build_sparse_vector(chunk),
                            },
                            payload={
                                "text": chunk,
                                "source_url": url,
                                "category": "jobber",
                                "document_id": doc_id,
                                "chunk_index": 0,
                                "last_updated": "",
                                **extra,
                            },
                        )
                    )
                await _asyncio.to_thread(
                    qdrant.upsert,
                    collection_name=settings.qdrant_collection,
                    points=points,
                    wait=True,
                )
                chunks_total += len(points)
                clients_total += len(batch)
        logger.info("Jobber sync: %d clients, %d chunks", clients_total, chunks_total)
        return {"synced_clients": clients_total, "chunks": chunks_total}
    finally:
        await session.close()
        await jobber.close()
        try:
            qdrant.close()
        except Exception as exc:
            logger.warning("Qdrant client close failed: %s", exc)


@app.task(name="texponse.tasks.health_check")
def health_check_task() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "redis_url": settings.redis_url,
        "qdrant_url": settings.qdrant_url,
    }
