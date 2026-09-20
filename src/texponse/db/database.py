from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from texponse.config import get_settings

_async_engine: AsyncEngine | None = None
_engine_loop: Any | None = None


def get_async_engine() -> AsyncEngine:
    """Return a process-wide engine, recreated when the event loop changes.

    Celery tasks run under ``asyncio.run()`` (one fresh loop per task), so a
    cached engine whose asyncpg connections belong to a previous loop raises
    "attached to a different loop". NullPool keeps no persistent connections,
    and the loop check recreates the engine whenever the loop rotates.
    """
    global _async_engine, _engine_loop
    try:
        running_loop: Any | None = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if _async_engine is not None and _engine_loop is not running_loop:
        _async_engine = None
    if _async_engine is not None:
        return _async_engine
    settings = get_settings()
    _async_engine = create_async_engine(
        settings.database_url, echo=settings.debug, poolclass=NullPool
    )
    _engine_loop = running_loop
    return _async_engine


def get_async_session() -> AsyncSession:
    return AsyncSession(get_async_engine(), expire_on_commit=False)


async def create_all() -> None:
    import texponse.db.models  # noqa: F401  # ensure tables are registered on metadata

    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
