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
    qdrant_use_tls: bool = False

    # ── Aircall ──────────────────────────────────────────────────────────
    aircall_api_id: str = ""
    aircall_api_token: str = ""
    aircall_webhook_secret: str = ""
    aircall_base_url: str = "https://api.aircall.io/v1"
    aircall_line_id: int | None = None
    aircall_callback_token: str = ""

    # ── Jobber ───────────────────────────────────────────────────────────
    jobber_client_id: str = ""
    jobber_client_secret: str = ""
    jobber_redirect_uri: str = ""
    jobber_base_url: str = "https://api.getjobber.com/api/graphql"
    jobber_graphql_version: str = "2025-04-16"

    # ── LLM provider ───────────────────────────────────────────────
    # "gemini" = hosted, free without card, ~1500 req/day, OpenAI-compatible
    # endpoint (default: best rate-limit story of the hosted options).
    # "openrouter" = hosted (free tier capped at ~50 req/day).
    # "ollama" = local, genuinely free/unlimited, non-reasoning instruct models.
    # When ollama, base_url should be http://ollama:11434/v1 (docker) or
    # http://localhost:11434/v1 (host) and models are e.g. "llama3.1:8b".
    # "free-proxy" = kaiser-data/free-llm-proxy-router sidecar: one local
    # endpoint that fails over across YOUR free-tier keys (Groq, Gemini,
    # OpenRouter, Cerebras, ...). Model is a routing strategy ("small" is
    # best for SMS: 3-13B fast models). Requires at least one provider key.
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_default_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = "gemini-3.5-flash"
    # Gemini meters 20 free requests/day PER MODEL, so a drained model must
    # rotate to a sibling with an untouched bucket instead of serving a
    # static SMS. Tried in order; each entry has its own 20/day quota.
    gemini_model_pool: str = (
        "gemini-3.8-flash,gemini-3.5-flash,gemini-3.6-flash,gemini-3.7-flash,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-latest"
    )
    gemini_timeout: float = 60.0
    # Gemini 3.x flash reasons by default and burns the token budget on
    # hidden thinking. "none" = answer directly (right for SMS).
    # Empty string disables the flag.
    gemini_reasoning_effort: str = "none"
    ollama_base_url: str = "http://ollama:11434/v1"
    ollama_default_model: str = "llama3.1:8b"
    ollama_fallback_model: str = "qwen2.5:7b"
    ollama_timeout: float = 120.0
    free_proxy_base_url: str = "http://free-llm-proxy:8080/v1"
    free_proxy_default_model: str = "small"
    free_proxy_fallback_model: str = "speed"
    free_proxy_timeout: float = 120.0

    # ── Shared LLM quota protection ────────────────────────────────
    # Min seconds between LLM calls across ALL worker threads (shared lane).
    # Seconds to park all LLM traffic after a 429.
    # Max billable LLM attempts per UTC day (Redis counter); over-budget
    # messages get the static SMS without touching the provider.
    llm_min_interval: float = 1.5
    llm_cooldown_seconds: float = 90.0
    llm_daily_budget: int = 40

    @property
    def gemini_models(self) -> list[str]:
        """Ordered model pool, drained-model rotation."""
        pool = [m.strip() for m in self.gemini_model_pool.split(",") if m.strip()]
        for m in (self.gemini_default_model, self.gemini_fallback_model):
            if m and m not in pool:
                pool.append(m)
        return pool

    @property
    def effective_daily_budget(self) -> int:
        """Daily LLM attempt cap, scaled to the active provider's free tier.

        Measured reality (Sep 2026), not marketing: Gemini free tier is
        20 requests PER DAY PER MODEL (429 RESOURCE_EXHAUSTED with
        retryDelay ~5h once hit) — far below the advertised 1500/day.
        Local Ollama is uncapped. So cap Gemini conservatively and let
        the multi-model fallback cover the rest.
        """
        if self.llm_provider == "gemini":
            # Hard provider limit is 20/day/model; stay under it so the
            # fallback model still has its own quota to serve.
            return 18
        if self.llm_provider in ("ollama", "free-proxy"):
            return 10_000  # local/pooled: effectively uncapped
        return self.llm_daily_budget  # openrouter: 50/day

    @property
    def active_default_model(self) -> str:
        if self.llm_provider == "gemini":
            return self.gemini_default_model
        if self.llm_provider == "ollama":
            return self.ollama_default_model
        if self.llm_provider == "free-proxy":
            return self.free_proxy_default_model
        return self.openrouter_default_model

    @property
    def active_fallback_model(self) -> str:
        if self.llm_provider == "gemini":
            return self.gemini_fallback_model
        if self.llm_provider == "ollama":
            return self.ollama_fallback_model
        if self.llm_provider == "free-proxy":
            return self.free_proxy_fallback_model
        return self.openrouter_fallback_model

    # ── OpenRouter ───────────────────────────────────────────────────────
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_default_model: str = "google/gemma-4-31b-it:free"
    openrouter_fallback_model: str = "liquid/lfm-2.5-2.6b:free"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    openrouter_timeout: float = 60.0

    # ── Local embeddings (FastEmbed, no API quota) ─────────────────────
    embedder_model: str = "BAAI/bge-small-en-v1.5"

    # ── Celery ───────────────────────────────────────────────────────────
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # ── Phone-hash pepper (caller isolation) ───────────────────────────
    # Primary pepper for new hashes. Rotation: set _previous to the old value,
    # re-sync Jobber data, then clear _previous. Falls back to secret_key if unset.
    phone_hash_pepper: str = ""
    phone_hash_pepper_previous: str = ""

    # ── Dev-only chunk-security bypass ────────────────────────────────
    # NEVER enable in production: skips the caller phone-hash check so devs
    # can test retrieval with any number. Company isolation stays enforced.
    # Hard-gated to app_env == "development" at use site.
    dev_bypass_chunk_security: bool = False

    # ── Dev god mode: disable ALL access security ─────────────────────
    # When true AND app_env == "development": skips Qdrant caller/company
    # filters and post-retrieval verification entirely — every chunk is
    # retrievable by anyone. Ignored outside development.
    # Output safety (PII redaction, injection refusal) stays ON even here:
    # dev still holds 1000 real client records, so outbound guards remain.
    dev_disable_security: bool = False

    # ── Dev: skip Jobber sync when Qdrant already has data ──────────
    # In development, re-syncing 1000 clients on every restart/beat is
    # wasteful. When app_env == "development" and the collection already
    # holds >= this many points, the sync task returns early as skipped.
    # 0 = always sync. Ignored outside development.
    dev_skip_sync_min_chunks: int = 500

    # ── Dispatcher bypass allowlist ───────────────────────────────────
    # Comma-separated phone numbers (any format; matched on last 10 digits)
    # allowed to look up ANY client within their own company. Company
    # isolation stays enforced. Empty = no bypass.
    # e.g. CHUNK_SECURITY_BYPASS_NUMBERS="+15551234567,+15557654321"
    chunk_security_bypass_numbers: str = ""

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
        scheme = "https" if self.qdrant_use_tls else "http"
        return f"{scheme}://{self.qdrant_host}:{self.qdrant_port}"

    @field_validator("secret_key")
    @classmethod
    def _require_prod_secret(cls, value: str) -> str:
        import os

        is_prod = os.getenv("APP_ENV", "").lower() == "production"
        if is_prod and (
            not value or value in {"dev-secret-change-me", "change-this-in-production"}
        ):
            raise ValueError("SECRET_KEY must be set to a strong value in production")
        return value

    def validate_production(self) -> list[str]:
        """Return list of missing/insecure settings blocking production deploy."""
        issues: list[str] = []
        if not self.openrouter_api_key:
            issues.append("OPENROUTER_API_KEY is empty")
        if not self.aircall_api_id or not self.aircall_api_token:
            issues.append("AIRCALL_API_ID / AIRCALL_API_TOKEN are empty")
        if not self.aircall_webhook_secret:
            issues.append("AIRCALL_WEBHOOK_SECRET is empty")
        if not self.aircall_callback_token:
            issues.append("AIRCALL_CALLBACK_TOKEN is empty (unsigned callbacks accepted)")
        if not self.jobber_client_id or not self.jobber_client_secret:
            issues.append("JOBBER client id/secret empty — no customer context")
        if self.secret_key in {"dev-secret-change-me", "change-this-in-production"}:
            issues.append("SECRET_KEY is still the dev default")
        if self.debug or self.app_env.lower() != "production":
            issues.append("DEBUG should be false and APP_ENV=production in prod")
        if self.dev_bypass_chunk_security:
            issues.append("DEV_BYPASS_CHUNK_SECURITY must be false in prod")
        return issues

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
