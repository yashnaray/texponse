from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status

from texponse.security.rbac import RBACMiddleware

router = APIRouter(prefix="/kpis", tags=["kpis"])


async def _current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def _require_kpi_access(user: object) -> None:
    role = str(getattr(user, "role", "") or "").lower().split(".")[-1]
    if role not in {"superuser", "dev", "admin", "system"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Superuser role required for KPIs"
        )


@router.get("", status_code=200)
async def list_kpis(request: Request) -> dict:
    from texponse.rag.kpis import KPI_DEFINITIONS

    user = await _current_user(request)
    _require_kpi_access(user)
    return {"kpis": [{**d.__dict__} for d in KPI_DEFINITIONS]}


def _validate_series_params(
    kpi: list[str] | None, granularity: str, start: str | None, end: str | None
) -> None:
    from texponse.rag.kpis import GRANULARITIES, KPI_DEFINITIONS, generate_windows

    valid_keys = {d.key for d in KPI_DEFINITIONS}
    if kpi and (bad := [k for k in kpi if k not in valid_keys]):
        raise HTTPException(status_code=422, detail=f"Unknown KPI keys: {bad}")
    if granularity.lower() not in GRANULARITIES:
        raise HTTPException(status_code=422, detail=f"granularity must be one of {GRANULARITIES}")
    if start is not None and end is not None:
        try:
            generate_windows(start, end, granularity)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    elif (start is None) != (end is None):
        raise HTTPException(status_code=422, detail="start and end must be provided together")


async def _windowed_kpis(
    company_id: str,
    start: str,
    end: str,
    granularity: str,
    kpi: list[str] | None,
    window_days: int,
) -> dict:
    """Aggregate KPIs over [start, end) plus per-bucket series."""
    import contextlib
    from datetime import date

    from qdrant_client import QdrantClient

    from texponse.config import get_settings
    from texponse.db.database import get_async_session
    from texponse.rag.kpis import (
        KPI_DEFINITIONS,
        _as_date,
        compute_kpi_series,
        compute_local_kpis,
        generate_windows,
        load_call_windows,
        load_local_records,
    )

    settings = get_settings()
    session = get_async_session()
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        records = await load_local_records(
            qdrant, session, settings.qdrant_collection, company_id, settings
        )
        windows = generate_windows(start, end, granularity.lower())
        calls_by_window = await load_call_windows(session, company_id, windows)
    finally:
        with contextlib.suppress(Exception):
            qdrant.close()
        await session.close()

    ws = date.fromisoformat(windows[0]["start"])
    we = date.fromisoformat(windows[-1]["end"])

    def _in(d: object) -> bool:
        dd = _as_date(d)
        return dd is not None and ws <= dd < we

    from texponse.rag.kpis import _record_date

    f_jobs = [j for j in records["jobs"] if _in(_record_date(j))]
    f_quotes = [q for q in records["quotes"] if _in(q.get("created_at"))]
    f_invoices = [
        v
        for v in records["invoices"]
        if _in(_as_date(v.get("due_date")) or _as_date(v.get("created_at")))
    ]
    f_contacts = [c for c in records.get("contacts", []) if _in(c.get("first_seen"))]
    total_calls = {
        "total_inbound": sum(w.get("total_inbound", 0) for w in calls_by_window.values()),
        "unanswered": sum(w.get("unanswered", 0) for w in calls_by_window.values()),
    }
    try:
        values = compute_local_kpis(
            f_jobs,
            f_quotes,
            f_invoices,
            total_calls,
            records.get("clients"),
            f_contacts,
            window_days=window_days,
        )
        series = compute_kpi_series(
            records["jobs"],
            records["quotes"],
            records["invoices"],
            calls_by_window,
            records.get("clients"),
            records.get("contacts"),
            start=start,
            end=end,
            granularity=granularity.lower(),
            kpi_keys=kpi,
            window_days=window_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if kpi:
        values = {k: values.get(k) for k in kpi}
    return {
        "company_id": company_id,
        "source": "local",
        "start": start,
        "end": end,
        "granularity": granularity.lower(),
        "window_days": window_days,
        "values": values,
        "series": series,
        "definitions": [{**d.__dict__} for d in KPI_DEFINITIONS if kpi is None or d.key in kpi],
        "counts": {
            "jobs": len(f_jobs),
            "quotes": len(f_quotes),
            "invoices": len(f_invoices),
            "contacts": len(f_contacts),
        },
    }


@router.get("/{company_id}", status_code=200)
async def company_kpis(
    request: Request,
    company_id: str,
    start: str | None = Query(default=None, description="Window start (ISO date, inclusive)"),  # noqa: B008
    end: str | None = Query(default=None, description="Window end (ISO date, exclusive)"),  # noqa: B008
    granularity: str = Query("month", description="day | week | month | year"),  # noqa: B008
    kpi: list[str] | None = Query(default=None, description="Filter to KPI keys (repeatable)"),  # noqa: B008
    window_days: int = Query(14, ge=1, le=365, description="Lead-booking conversion window"),  # noqa: B008
) -> dict:
    """KPIs aggregated from local synced data (Qdrant + Postgres).

    Zero Jobber API calls: reads the structured kpi_job / kpi_quote /
    kpi_invoice points written by the hourly sync. Unavailable KPIs report
    null ("n/a") instead of fabricated numbers.

    With ?start=&end= the KPIs aggregate over that window and include a
    per-bucket series (daily/weekly/monthly/yearly); without them the
    response covers all synced data as a single period.
    """
    from qdrant_client import QdrantClient

    from texponse.config import get_settings
    from texponse.db.database import get_async_session
    from texponse.rag.kpis import (
        KPI_DEFINITIONS,
        compute_local_kpis,
        load_local_records,
    )

    user = await _current_user(request)
    _require_kpi_access(user)
    if not RBACMiddleware.can_access_company(user, company_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    _validate_series_params(kpi, granularity, start, end)

    if start is not None and end is not None:
        return await _windowed_kpis(company_id, start, end, granularity, kpi, window_days)

    settings = get_settings()
    session = get_async_session()
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    import contextlib

    try:
        records = await load_local_records(
            qdrant, session, settings.qdrant_collection, company_id, settings
        )
    finally:
        with contextlib.suppress(Exception):
            qdrant.close()
        await session.close()
    values = compute_local_kpis(
        records["jobs"],
        records["quotes"],
        records["invoices"],
        records["calls"],
        records.get("clients"),
        records.get("contacts"),
        window_days=window_days,
    )
    if kpi:
        values = {k: values.get(k) for k in kpi}
    return {
        "company_id": company_id,
        "source": "local",
        "values": values,
        "definitions": [{**d.__dict__} for d in KPI_DEFINITIONS if kpi is None or d.key in kpi],
        "counts": {
            "jobs": len(records["jobs"]),
            "quotes": len(records["quotes"]),
            "invoices": len(records["invoices"]),
        },
    }


@router.get("/{company_id}/series", status_code=200)
async def company_kpis_series(
    request: Request,
    company_id: str,
    start: str = Query(..., description="Window start (ISO date/datetime, inclusive)"),  # noqa: B008
    end: str = Query(..., description="Window end (ISO date/datetime, exclusive)"),  # noqa: B008
    granularity: str = Query("month", description="day | week | month | year"),  # noqa: B008
    kpi: list[str] | None = Query(default=None, description="Filter to KPI keys (repeatable)"),  # noqa: B008
    window_days: int = Query(14, ge=1, le=365, description="Lead-booking conversion window"),  # noqa: B008
) -> dict:
    """KPIs bucketed over [start, end) — e.g. daily/monthly/yearly booking rates.

    Example: /kpis/{id}/series?start=2024-01-01&end=2025-01-01&granularity=month
    """
    user = await _current_user(request)
    _require_kpi_access(user)
    if not RBACMiddleware.can_access_company(user, company_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    _validate_series_params(kpi, granularity, start, end)

    return await _windowed_kpis(company_id, start, end, granularity, kpi, window_days)


def get_router() -> APIRouter:
    return router


# Back-compat for `from texponse.api.kpis import router`.
__all__ = ["router", "get_router"]
