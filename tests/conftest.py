from __future__ import annotations

import pytest
import respx
from qdrant_client import QdrantClient

from texponse.config import Settings
from texponse.integrations.openrouter import OpenRouterClient

TEST_SETTINGS = Settings(
    app_env="testing",
    debug=False,
    log_level="ERROR",
    secret_key="test-secret-key",
    postgres_host="localhost",
    postgres_port=5432,
    postgres_db="texponse_test",
    postgres_user="texponse_test",
    postgres_password="test",
    redis_host="localhost",
    redis_port=6379,
    redis_password="",
    redis_database=1,
    qdrant_host="localhost",
    qdrant_port=6333,
    aircall_api_id="test_api_id",
    aircall_api_token="test_api_token",
    aircall_webhook_secret="test_webhook_secret",
    jobber_client_id="test_jobber_client",
    jobber_client_secret="test_jobber_secret",
    jobber_redirect_uri="http://localhost:8000/auth/jobber/callback",
    openrouter_api_key="sk-test-key",
    openrouter_base_url="https://openrouter.ai/api/v1",
    openrouter_default_model="meta-llama/llama-3.1-8b-instruct:free",
    openrouter_fallback_model="mistralai/mistral-7b-instruct:free",
    openrouter_embedding_model="openai/text-embedding-3-small",
    openrouter_timeout=30.0,
    database_url="sqlite+aiosqlite:///:memory:",
)


@pytest.fixture
def settings() -> Settings:
    return TEST_SETTINGS


@pytest.fixture
def mock_aircall_signature():
    import hashlib
    import hmac

    secret = TEST_SETTINGS.aircall_api_token
    payload = b'{"event": "message.received", "resource": {"id": "123"}}'
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return payload, sig


@pytest.fixture
async def async_session():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(engine) as session:
        yield session
        await session.rollback()


@pytest.fixture
def qdrant_client():
    client = QdrantClient(
        url="http://localhost:6333",
        api_key=None,
    )
    yield client


@pytest.fixture
@respx.mock
async def openrouter_mock(settings):
    client = OpenRouterClient(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        default_model=settings.openrouter_default_model,
        fallback_model=settings.openrouter_fallback_model,
    )
    yield client
    await client._client.close()
