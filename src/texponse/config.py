from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────
    app_env: str = "development"
    app_name: str = "texponse"
    debug: bool = False
    log_level: str = "INFO"
    secret_key: str = "dev-secret-change-me"

    # ── FastAPI ──────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ── PostgreSQL ───────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "texponse"
    postgres_user: str = "texponse"
    postgres_password: str = "texponse"

    # ── Redis ────────────────────────────────────────────────────────────
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = "texponse_dev"
    redis_database: int = 0

    # ── Qdrant ───────────────────────────────────────────────────────────
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_api_key: str | None = None
    qdrant_collection: str = "knowledge_base"

    # ── Aircall ──────────────────────────────────────────────────────────
    aircall_api_id: str = ""
    aircall_api_token: str = ""
    aircall_webhook_secret: str = ""
    aircall_base_url: str = "https://api.aircall.io/v1"
    aircall_line_id: int | None = None

    # ── Jobber ───────────────────────────────────────────────────────────
    jobber_client_id: str = ""
    jobber_client_secret: str = ""
    jobber_redirect_uri: str = ""
    jobber_base_url: str = "https://api.getjobber.com/api/graphql"
    jobber_graphql_version: str = "2025-04-16"

    # ── OpenRouter ───────────────────────────────────────────────────────
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_default_model: str = "openrouter/free"
    openrouter_fallback_model: str = "google/gemma-4-31b-it:free"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    openrouter_timeout: float = 60.0

    # ── Celery ───────────────────────────────────────────────────────────
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # ── Rate Limiting ───────────────────────────────────────────────────
    webhook_rate_limit: str = "60/minute"
    api_rate_limit: str = "120/minute"

    # ── Model config ─────────────────────────────────────────────────────
    max_history_tokens: int = 2048
    max_passage_tokens: int = 1024
    response_token_reserve: int = 512
    max_response_tokens: int = 512
    rag_top_k: int = 10

    @field_validator("aircall_line_id", mode="before")
    @classmethod
    def _empty_str_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_database}"

    @property
    def qdrant_url(self) -> str:
        return f"http://{self.qdrant_host}:{self.qdrant_port}"

    @property
    def openrouter_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://texponse.ai",
            "X-OpenRouter-Title": "Texponse",
        }

    @property
    def celery_broker(self) -> str:
        if self.celery_broker_url:
            return self.celery_broker_url
        return self.redis_url

    @property
    def celery_backend(self) -> str:
        if self.celery_result_backend:
            return self.celery_result_backend
        return self.redis_url

    @property
    def jobber_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Jobber-GraphQL-Version": self.jobber_graphql_version,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
