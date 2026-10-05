from __future__ import annotations

from datetime import datetime

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
async def company_kpis(request: Request, company_id: str,
                       start: str | None = None, end: str | None = None) -> dict:
    from texponse.config import get_settings
    from texponse.db.database import get_async_session
    from texponse.db.repositories import JobberTokenRepository
    from texponse.integrations.jobber import JobberClient
    from texponse.rag.kpis import JobberKPIService

    user = await _current_user(request)
    _require_kpi_access(user)
    if not RBACMiddleware.can_access_company(user, company_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    def _parse(v: str | None) -> datetime | None:
        return datetime.fromisoformat(v) if v else None

    session = get_async_session()
    try:
        repo = JobberTokenRepository(session)
        token = await repo.get(company_id)
        if token is None:
            raise HTTPException(status_code=404, detail="No Jobber connection for company")
        settings = get_settings()
        client = JobberClient(client_id=settings.jobber_client_id,
                              client_secret=settings.jobber_client_secret,
                              redirect_uri=settings.jobber_redirect_uri,
                              graphql_version=settings.jobber_graphql_version,
                              base_url=settings.jobber_base_url)
        try:
            svc = JobberKPIService(client.graphql, token.access_token)
            result = await svc.get_kpis(_parse(start), _parse(end))
        finally:
            await client.close()
    finally:
        await session.close()
    return {"company_id": company_id, **result}


def get_router() -> APIRouter:
    return router


# Back-compat for `from texponse.api.kpis import router`.
__all__ = ["router", "get_router"]
