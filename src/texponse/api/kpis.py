from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Superuser role required for KPIs")


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
        records["jobs"], records["quotes"], records["invoices"], records["calls"],
        records.get("clients"), records.get("contacts"),
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


def get_router() -> APIRouter:
    return router


# Back-compat for `from texponse.api.kpis import router`.
__all__ = ["router", "get_router"]
