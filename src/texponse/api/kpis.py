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


@router.get("/{company_id}", status_code=200)
async def company_kpis(request: Request, company_id: str) -> dict:
    """KPIs aggregated from local synced data (Qdrant + Postgres).

    Zero Jobber API calls: reads the structured kpi_job / kpi_quote /
    kpi_invoice points written by the hourly sync. Unavailable KPIs report
    null ("n/a") instead of fabricated numbers.
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
    )
    return {
        "company_id": company_id,
        "source": "local",
        "values": values,
        "definitions": [{**d.__dict__} for d in KPI_DEFINITIONS],
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
    from qdrant_client import QdrantClient

    from texponse.config import get_settings
    from texponse.db.database import get_async_session
    from texponse.rag.kpis import (
        GRANULARITIES,
        KPI_DEFINITIONS,
        compute_kpi_series,
        generate_windows,
        load_call_windows,
        load_local_records,
    )

    user = await _current_user(request)
    _require_kpi_access(user)
    if not RBACMiddleware.can_access_company(user, company_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    valid_keys = {d.key for d in KPI_DEFINITIONS}
    if kpi and (bad := [k for k in kpi if k not in valid_keys]):
        raise HTTPException(status_code=422, detail=f"Unknown KPI keys: {bad}")
    if granularity.lower() not in GRANULARITIES:
        raise HTTPException(status_code=422, detail=f"granularity must be one of {GRANULARITIES}")
    try:
        generate_windows(start, end, granularity)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    settings = get_settings()
    session = get_async_session()
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    import contextlib

    try:
        records = await load_local_records(
            qdrant, session, settings.qdrant_collection, company_id, settings
        )
        windows = generate_windows(start, end, granularity)
        calls_by_window = await load_call_windows(session, company_id, windows)
    finally:
        with contextlib.suppress(Exception):
            qdrant.close()
        await session.close()
    try:
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
    return {
        "company_id": company_id,
        "source": "local",
        "start": start,
        "end": end,
        "granularity": granularity.lower(),
        "window_days": window_days,
        "series": series,
        "definitions": [{**d.__dict__} for d in KPI_DEFINITIONS if kpi is None or d.key in kpi],
    }


def get_router() -> APIRouter:
    return router


# Back-compat for `from texponse.api.kpis import router`.
__all__ = ["router", "get_router"]
