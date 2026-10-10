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

    # ── TCPA/CTIA SMS compliance ─────────────────────────────────────
    # Keyword handling + suppression-list enforcement for SMS. Voice calls
    # are unaffected. Opt-out/HELP/opt-in get deterministic canned replies
    # (no LLM) so behavior is auditable; suppressed callers get silence.
    from texponse.db.models import SmsConsentStatus
    from texponse.db.repositories import CompanyRepository, SmsSuppressionRepository
    from texponse.security import tcpa as _tcpa
    from texponse.security.guardrails import active_peppers as _tcpa_peppers
    from texponse.security.guardrails import phone_hash as _tcpa_hash

    _is_sms = event_type.startswith("message.")
    _suppression_repo = SmsSuppressionRepository(session)
    _tcpa_hashes: list[str] = []
    if _is_sms and from_number:
        _all_peppers = _tcpa_peppers(
            settings.secret_key,
            settings.phone_hash_pepper,
            settings.phone_hash_pepper_previous,
        )
        _tcpa_hashes = sorted({_tcpa_hash(from_number, pepper=p) for p in _all_peppers} - {""})

    async def _tcpa_send(text: str, *, force: bool = False) -> str | None:
        """Send an SMS honoring the suppression list. Returns delivery_error or None."""
        if not from_number:
            return "missing phone numbers"
        if settings.aircall_line_id is None:
            return "AIRCALL_LINE_ID is not configured"
        if (
            not force
            and _tcpa_hashes
            and await _suppression_repo.is_suppressed(company_id, _tcpa_hashes)
        ):
            logger.info(
                "TCPA suppression: skipping send to opted-out caller (company=%s)", company_id
            )
            return "suppressed: recipient opted out"
        try:
            await aircall.send_message(
                line_id=settings.aircall_line_id,
                to_number=from_number,
                content=text[:1500],
            )
        except Exception as exc:
            import contextlib as _ctx

            detail = str(exc)
            http_response = getattr(exc, "response", None)
            if http_response is not None:
                with _ctx.suppress(Exception):
                    detail = f"{exc} | body: {http_response.text[:500]}"
            return detail
        try:
            from texponse.db.models import ConversationEvent as _CE
            from texponse.db.models import ConversationEventType as _CET

            await repo.add_event(
                _CE(
                    conversation_id=conv.id,
                    event_type=_CET.MESSAGE_SENT,
                    from_number=to_number or None,
                    to_number=from_number or None,
                    content=text[:1500],
                    direction="outbound",
                    raw_payload={"tcpa": True},
                )
            )
        except Exception as exc:
            logger.warning("Outbound SMS audit event failed (non-fatal): %s", exc)
        return None

    async def _tcpa_short_circuit(action: str, reply: str, *, force: bool) -> dict[str, Any]:
        delivery_error = await _tcpa_send(reply, force=force)
        res: dict[str, Any] = {
            "conversation_id": conv.id,
            "response_preview": reply[:200],
            "model_used": "tcpa-canned",
            "tokens": {"prompt": 0, "completion": 0, "total": 0},
            "latency_ms": 0,
            "was_fallback": False,
            "passages_retrieved": 0,
            "tcpa": action,
        }
        if delivery_error is not None:
            res["delivery_error"] = delivery_error
        return res

    if _is_sms and from_number and _tcpa_hashes:
        _company = await CompanyRepository(session).get(company_id)
        _sender = _company.name if _company and _company.name else "Texponse"
        _keyword = _tcpa.classify_sms_keyword(content)
        _raw_keyword = (content or "").strip().split()[0].lower() if (content or "").strip() else ""
        if _keyword == _tcpa.SmsKeyword.OPT_OUT:
            await _suppression_repo.set_status(
                company_id, _tcpa_hashes, SmsConsentStatus.OPTED_OUT, keyword=_raw_keyword
            )
            logger.info("TCPA opt-out recorded (company=%s, keyword=%s)", company_id, _raw_keyword)
            return await _tcpa_short_circuit(
                "opted_out", _tcpa.opt_out_confirmation(_sender), force=True
            )
        if _keyword == _tcpa.SmsKeyword.OPT_IN:
            await _suppression_repo.set_status(
                company_id, _tcpa_hashes, SmsConsentStatus.OPTED_IN, keyword=_raw_keyword
            )
            logger.info("TCPA opt-in recorded (company=%s, keyword=%s)", company_id, _raw_keyword)
            return await _tcpa_short_circuit(
                "opted_in", _tcpa.opt_in_confirmation(_sender), force=True
            )
        if _keyword == _tcpa.SmsKeyword.HELP:
            return await _tcpa_short_circuit("help", _tcpa.help_reply(_sender), force=True)
        if await _suppression_repo.is_suppressed(company_id, _tcpa_hashes):
            logger.info("TCPA suppression: ignoring message from opted-out caller")
            return await _tcpa_short_circuit("suppressed", "", force=False)

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
            "DEV GOD MODE: all chunk access security disabled. Never enable outside development."
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
            logger.exception(
                "Qdrant retrieve failed (collection=%s): %s", settings.qdrant_collection, exc
            )
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

    # Business context comes from Qdrant point metadata (written by the
    # hourly Jobber sync) — no live Jobber API call per message, so no
    # throttling. See _qdrant_client_context below.
    jobber_context: dict[str, Any] | None = None
    if from_number and _hashes:
        try:
            jobber_context = await _qdrant_client_context(
                qdrant, settings.qdrant_collection, company_id, _hashes
            )
        except Exception as exc:
            logger.warning("Qdrant client-context lookup failed: %s", exc)

    # KPI questions (e.g. "what's our booking rate?") get live numbers.
    # Gated to staff lines (dispatcher allowlist / dev bypass): revenue and
    # margin figures must never leak to regular customers who ask.
    kpi_context: str | None = None
    if query and (_bypass or _god_mode):
        from texponse.rag.kpis import detect_kpi_intent

        matched = detect_kpi_intent(query)
        if matched:
            try:
                kpi_context = await _get_cached_kpi_answer(settings, session, company_id, matched)
            except Exception as exc:
                logger.warning("KPI lookup failed, answering without KPIs: %s", exc)
    elif query:
        from texponse.rag.kpis import detect_kpi_intent as _detect

        if _detect(query):
            logger.info("KPI question from non-staff caller; withholding numbers.")

    result_fallback_note: str | None = None

    prompt_context = PromptContext(
        passages=passages,
        conversation_history=history_msgs,
        jobber_context=jobber_context,
        customer_name=jobber_context.get("name") if jobber_context else None,
        customer_phone=from_number,
        kpi_context=kpi_context,
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

    # Suppression re-checked here (race-safe): a STOP arriving mid-LLM
    # still blocks this send. Audit event recorded on success.
    delivery_error = await _tcpa_send(response_content)
    if delivery_error is not None:
        logger.warning("Aircall send skipped/failed: %s", delivery_error)
        result["delivery_error"] = delivery_error

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
    budget = int(
        getattr(settings, "effective_daily_budget", None)
        or getattr(settings, "llm_daily_budget", 40)
        or 0
    )
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


KPI_CACHE_TTL = 3600  # seconds; KPI answers refresh at most hourly per company


async def _get_cached_kpi_answer(
    settings: Any,
    session: AsyncSession,
    company_id: str,
    matched: list[Any],
) -> str | None:
    """Return formatted KPI text aggregated from LOCAL data only.

    Reads the structured kpi_job / kpi_quote / kpi_invoice points written by
    the hourly sync plus Aircall call stats from Postgres — zero Jobber API
    calls. Results cached in Redis for an hour per company.
    """
    import json as _json

    from texponse.rag.kpis import compute_local_kpis
    from texponse.rag.kpis import format_kpi_answer as _fmt

    keys = sorted(d.key for d in matched)
    cache_key = f"texponse:kpis:local:{company_id}:{','.join(keys)}"
    try:
        import redis.asyncio as _aioredis

        rc = _aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        try:
            hit = await rc.get(cache_key)
        finally:
            await rc.aclose()
        if hit:
            cached = _json.loads(hit)
            return _fmt(cached["values"], matched)
    except Exception as exc:
        logger.warning("KPI cache read failed, computing live: %s", exc)

    from qdrant_client import QdrantClient as _QC

    from texponse.rag.kpis import load_local_records

    qdrant = _QC(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        records = await load_local_records(
            qdrant, session, settings.qdrant_collection, company_id, settings
        )
    finally:
        try:
            qdrant.close()
        except Exception as exc:
            logger.warning("Qdrant client close failed: %s", exc)
    values = compute_local_kpis(
        records["jobs"],
        records["quotes"],
        records["invoices"],
        records["calls"],
        records.get("clients"),
        records.get("contacts"),
    )
    try:
        import redis.asyncio as _aioredis

        rc = _aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        try:
            await rc.setex(cache_key, KPI_CACHE_TTL, _json.dumps({"values": values}, default=str))
        finally:
            await rc.aclose()
    except Exception as exc:
        logger.warning("KPI cache write failed (non-fatal): %s", exc)
    return _fmt(values, matched)


async def _qdrant_client_context(
    qdrant: Any,
    collection: str,
    company_id: str,
    hashes: list[str],
) -> dict[str, Any] | None:
    """Fetch one client's business context from Qdrant point metadata.

    Matches points the sync wrote: category=jobber, this company_id, and a
    phone_hashes overlap with the caller. Returns a dict shaped like the old
    ``find_client_by_phone`` result so the prompt builder needs no changes.
    """
    import asyncio as _aio

    from qdrant_client.http import models as _qm

    filt = _qm.Filter(
        must=[
            _qm.FieldCondition(key="category", match=_qm.MatchValue(value="jobber")),
            _qm.FieldCondition(key="company_id", match=_qm.MatchValue(value=company_id)),
            _qm.FieldCondition(key="phone_hashes", match=_qm.MatchAny(any=hashes)),
        ]
    )
    points, _ = await _aio.to_thread(
        qdrant.scroll, collection_name=collection, scroll_filter=filt, limit=1
    )
    if not points:
        return None
    payload = points[0].payload or {}
    # Legacy points (written before jobs_snapshot existed) carry everything
    # in the redacted text field — pass it through as notes so they still
    # provide context until the next hourly sync rewrites them.
    if not payload.get("client_name") and payload.get("text"):
        return {
            "id": payload.get("document_id", ""),
            "name": "",
            "companyName": "",
            "balance": 0,
            "isLead": False,
            "jobs": [],
            "notes": payload.get("text", ""),
            "source": payload.get("source_url", ""),
        }
    jobs = payload.get("jobs_snapshot", []) or []
    return {
        "id": payload.get("client_id", ""),
        "name": payload.get("client_name", ""),
        "companyName": payload.get("client_company", ""),
        "balance": payload.get("balance", 0),
        "isLead": payload.get("is_lead", False),
        "jobs": [
            {
                "title": j.get("title", ""),
                "jobStatus": j.get("jobStatus", ""),
                "total": j.get("total", 0),
            }
            for j in jobs
            if isinstance(j, dict)
        ],
        "source": payload.get("source_url", ""),
    }


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
                visits = [v for j in jobs for v in ((j.get("visits") or {}).get("nodes", []) or [])]
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
                            # Structured business context for per-message
                            # Qdrant metadata lookup (no Jobber API call).
                            "client_id": c.get("id", ""),
                            "client_name": c.get("name", ""),
                            "client_company": c.get("companyName", ""),
                            "balance": c.get("balance", 0),
                            "is_lead": c.get("isLead", False),
                            "jobs_snapshot": [
                                {
                                    "title": j.get("title", ""),
                                    "jobStatus": j.get("jobStatus", ""),
                                    "total": j.get("total", 0),
                                }
                                for j in jobs[:5]
                            ],
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

            # ── KPI objects: flat jobs/quotes/invoices with structured fields.
            # Stored as kpi_job / kpi_quote / kpi_invoice points so SMS KPI
            # questions aggregate from Qdrant metadata with zero Jobber calls.
            try:
                kpi_counts = await _sync_kpi_objects(
                    jobber,
                    qdrant,
                    _embedder,
                    _embed_batch,
                    settings,
                    token,
                    _uuid,
                    _Point,
                    _asyncio,
                )
                chunks_total += sum(kpi_counts.values())
            except Exception as exc:
                logger.warning("Jobber KPI sync failed for %s: %s", token.company_id, exc)
                kpi_counts = {}
        logger.info(
            "Jobber sync: %d clients, %d chunks (%s)", clients_total, chunks_total, kpi_counts
        )
        return {"synced_clients": clients_total, "chunks": chunks_total, "kpi": kpi_counts}
    finally:
        await session.close()
        await jobber.close()
        try:
            qdrant.close()
        except Exception as exc:
            logger.warning("Qdrant client close failed: %s", exc)


async def _upsert_kpi_points(
    qdrant: Any,
    embed_batch: Any,
    settings: Any,
    collection_docs: list[tuple[str, str, dict]],
    uuid_mod: Any,
    point_cls: Any,
    aio: Any,
    embedder_cls: Any,
) -> int:
    """Upsert pre-built (doc_id, text, payload) KPI docs in 32-doc batches."""
    total = 0
    for i in range(0, len(collection_docs), 32):
        batch = collection_docs[i : i + 32]
        vectors = await embed_batch([t for _, t, _ in batch])
        points = []
        for (doc_id, text, payload), vec in zip(batch, vectors, strict=True):
            points.append(
                point_cls(
                    id=uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, f"{doc_id}:0").hex,
                    vector={
                        "dense": vec,
                        "bm25": embedder_cls.build_sparse_vector(text),
                    },
                    payload=payload,
                )
            )
        await aio.to_thread(
            qdrant.upsert,
            collection_name=settings.qdrant_collection,
            points=points,
            wait=True,
        )
        total += len(points)
    return total


async def _sync_kpi_objects(
    jobber: Any,
    qdrant: Any,
    embedder: Any,
    embed_batch: Any,
    settings: Any,
    token: Any,
    uuid_mod: Any,
    point_cls: Any,
    aio: Any,
) -> dict[str, int]:
    """Fetch flat jobs/quotes/invoices and store as structured KPI points."""
    company_id = token.company_id
    counts: dict[str, int] = {}

    jobs = await jobber.list_all_jobs_flat(token.access_token)
    job_docs = [
        (
            f"kpi-job-{company_id}-{j.get('id', '')}",
            f"Job: {j.get('title', '')} ({j.get('jobStatus', '')}, "
            f"total ${j.get('total', 0)}, invoiced ${j.get('invoicedTotal', 0)})",
            {
                "category": "kpi_job",
                "company_id": company_id,
                "document_id": f"kpi-job-{company_id}-{j.get('id', '')}",
                "job_id": j.get("id", ""),
                "title": j.get("title", ""),
                "status": str(j.get("jobStatus", "") or "").lower(),
                "job_type": j.get("jobType", ""),
                "total": float(j.get("total") or 0),
                "invoiced_total": float(j.get("invoicedTotal") or 0),
                "completed_at": j.get("completedAt") or "",
                "created_at": j.get("createdAt") or j.get("startAt") or "",
                "source": j.get("source") or "",
                "jobber_client_id": (j.get("client") or {}).get("id", ""),
                "costing": j.get("jobCosting") or {},
            },
        )
        for j in jobs
    ]
    counts["jobs"] = await _upsert_kpi_points(
        qdrant,
        embed_batch,
        settings,
        job_docs,
        uuid_mod,
        point_cls,
        aio,
        type(embedder),
    )

    quotes = await jobber.list_all_quotes(token.access_token)
    quote_docs = [
        (
            f"kpi-quote-{company_id}-{q.get('id', '')}",
            f"Quote: {q.get('title', '')} ({q.get('quoteStatus', '')}, "
            f"total ${(q.get('amounts') or {}).get('total', 0)})",
            {
                "category": "kpi_quote",
                "company_id": company_id,
                "document_id": f"kpi-quote-{company_id}-{q.get('id', '')}",
                "quote_id": q.get("id", ""),
                "title": q.get("title", ""),
                "status": str(q.get("quoteStatus", "") or "").lower(),
                "total": float((q.get("amounts") or {}).get("total") or 0),
                "created_at": q.get("createdAt") or "",
            },
        )
        for q in quotes
    ]
    counts["quotes"] = await _upsert_kpi_points(
        qdrant,
        embed_batch,
        settings,
        quote_docs,
        uuid_mod,
        point_cls,
        aio,
        type(embedder),
    )

    invoices = await jobber.list_all_invoices(token.access_token)
    inv_docs = [
        (
            f"kpi-invoice-{company_id}-{v.get('id', '')}",
            f"Invoice: {v.get('invoiceStatus', '')}, net ${v.get('invoiceNet', 0)}, "
            f"due {v.get('dueDate', '')}",
            {
                "category": "kpi_invoice",
                "company_id": company_id,
                "document_id": f"kpi-invoice-{company_id}-{v.get('id', '')}",
                "invoice_id": v.get("id", ""),
                "status": str(v.get("invoiceStatus", "") or "").lower(),
                "net": float(v.get("invoiceNet") or 0),
                "due_date": v.get("dueDate") or "",
                "created_at": v.get("createdAt") or "",
            },
        )
        for v in invoices
    ]
    counts["invoices"] = await _upsert_kpi_points(
        qdrant,
        embed_batch,
        settings,
        inv_docs,
        uuid_mod,
        point_cls,
        aio,
        type(embedder),
    )
    return counts


@app.task(name="texponse.tasks.health_check")
def health_check_task() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "redis_url": settings.redis_url,
        "qdrant_url": settings.qdrant_url,
    }
