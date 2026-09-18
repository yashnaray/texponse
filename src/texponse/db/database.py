from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from texponse.config import get_settings

_async_engine: AsyncEngine | None = None


def get_async_engine() -> AsyncEngine:
    global _async_engine
    if _async_engine is not None:
        return _async_engine
    settings = get_settings()
    _async_engine = create_async_engine(settings.database_url, echo=settings.debug)
    return _async_engine


def get_async_session() -> AsyncSession:
    return AsyncSession(get_async_engine())


async def create_all() -> None:
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
