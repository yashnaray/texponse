# Technology Stack Recommendation

## Core Decisions

| Layer              | Choice                          | Rationale                                                     |
|--------------------|---------------------------------|--------------------------------------------------------------|
| **Language**       | Python 3.11+                    | Async-first syntax (`X \| Y`, `list[...]`), match statements |
| **Web Framework**  | FastAPI                         | Native async, OpenAPI-first, auto-generated docs, uvicorn     |
| **HTTP Client**    | httpx (async)                   | Async context managers, connection pooling, timeout control   |
| **Relational DB**  | PostgreSQL 15 + SQLModel        | Async engine via asyncpg, type-safe ORM, migrations via Alembic |
| **Vector DB**      | Qdrant                          | Native hybrid search (dense + sparse/BM25), REST + gRPC API   |
| **Embeddings**     | OpenRouter `/embeddings`        | Reuses same LLM provider stack, no extra service needed       |
| **LLM Orchestration** | OpenAI Python SDK @ OpenRouter | OpenAI-compatible API, works with free-tier models            |
| **Background Jobs** | Celery + Redis                 | Distributed task queue, battle-tested, handles retrying      |
| **Secrets**        | Pydantic Settings + env files   | `.env` for dev, sealed secrets / Vault for prod               |
| **Rate Limiting**  | slowapi + Redis token bucket    | Per-endpoint + per-integration rate limits                    |
| **Observability**  | OpenTelemetry + Prometheus      | Distributed tracing, metrics, structured logging              |
| **Packaging**      | `uv` + `pyproject.toml`         | Fast installs, deterministic resolution, modern Python tooling|
| **Containerization**| Docker + Docker Compose         | Reproducible dev/prod, supports K8s migration path            |
| **CI/CD**          | GitHub Actions                  | Linting, type-check, tests, build, publish                   |

## Dependency Catalogue

### Production dependencies

| Package                 | Purpose                                           |
|-------------------------|---------------------------------------------------|
| `fastapi`               | Web framework / webhook endpoints                 |
| `uvicorn[standard]`     | ASGI server                                       |
| `httpx[http2]`          | Async HTTP client for API calls                   |
| `sqlmodel`              | Async ORM (SQLAlchemy 2.0 + Pydantic)             |
| `asyncpg`               | PostgreSQL async driver                           |
| `alembic`               | Database migrations                               |
| `qdrant-client[fastembed]` | Qdrant client + sparse vector models            |
| `openai>=1.0`           | OpenRouter API client (OpenAI-compatible)         |
| `celery`                | Background task worker                            |
| `redis`                 | Redis client (broker + cache)                     |
| `pydantic-settings`     | Environment-based config                          |
| `pydantic`              | Data validation                                   |
| `slowapi`               | FastAPI rate limiting middleware                  |
| `tiktoken`              | Token counting for context-window budgeting       |
| `opentelemetry-api` + `opentelemetry-sdk` | Distributed tracing               |
| `opentelemetry-instrumentation-fastapi` | FastAPI tracing instrumentation     |
| `psutil`                | Worker memory/CPU monitoring                       |

### Linting / type-checking / formatting

| Package | Purpose           |
|---------|-------------------|
| `ruff`  | Lint + format     |
| `mypy`  | Static type check |
| `black` | Backup formatter  |

### Dev / test dependencies

| Package         | Purpose                         |
|-----------------|---------------------------------|
| `pytest`        | Test runner                     |
| `pytest-asyncio`| Async test support             |
| `pytest-httpx`  | HTTP mocking                   |
| `respx`         | httpx request mocking          |
| `fakeredis`     | In-memory Redis for tests      |
| `testcontainers`| Spin up Postgres/Qdrant for e2e|

## Why These Choices?

### FastAPI over Flask/Falcon
FastAPI provides **native async**, automatic OpenAPI schema generation, and dependency injection
that integrates cleanly with SQLModel. The async model means a single Uvicorn worker can handle
thousands of concurrent webhook requests without blocking.

### SQLModel over raw SQLAlchemy
SQLModel combines Pydantic models with SQLAlchemy 2.0, giving us type-safe DB models that
serialize directly to/from JSON for API responses. The repository pattern is still used for
query logic — routes never touch the ORM directly.

### Qdrant over Pinecone/Weaviate
Qdrant is **open-source and self-hostable**, supports **native hybrid search** (dense + sparse +
RRF fusion in one API call), and provides built-in BM25 via sparse vectors. This means we don't
need a separate search engine (e.g., Elasticsearch) for keyword retrieval.

### Celery over RQ/Dramatiq
Celery has mature retry semantics, rate limiting per task, and integrates with the Redis broker
already needed for caching. The `solo` pool gives us asyncio worker support.

### OpenRouter over direct model APIs
OpenRouter provides a **single OpenAI-compatible endpoint** with automatic fallback across
providers, free-tier model support, and no vendor lock-in. We can switch free models without
code changes.

## Free-Tier Model Selection Strategy

OpenRouter exposes many free models. The orchestrator selects based on task requirements:

| Task Type          | Primary (free) model                     | Fallback (free) model                  |
|--------------------|------------------------------------------|----------------------------------------|
| Short QA / summary | `meta-llama/llama-3.1-8b-instruct:free`  | `mistralai/mistral-7b-instruct:free`   |
| Code reasoning     | `meta-llama/llama-3.1-8b-instruct:free`  | `google/gemma-2-9b-it:free`            |
| Multilingual       | `meta-llama/llama-3.1-8b-instruct:free`  | `microsoft/phi-3-mini-128k-instruct:free` |

The `:free` suffix restricts OpenRouter to free-tier endpoints only.

## Infrastructure Layout (docker-compose)

```
┌─────────────────────────────────────────────────────┐
│  docker-compose.yml                                  │
├─────────────────────────────────────────────────────┤
│  app         — FastAPI + Uvicorn (port 8000)        │
│  worker      — Celery worker (same image, different  │
│                entrypoint)                          │
│  postgres    — PostgreSQL 15 (port 5432)            │
│  qdrant      — Qdrant (port 6333)                   │
│  redis       — Redis (port 6379)                    │
│  prometheus  — Metrics (port 9090)                  │
│  grafana     — Dashboards (port 3000)               │
│  otel-collector — Traces/metrics forwarder          │
└─────────────────────────────────────────────────────┘
```
