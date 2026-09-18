# Texponse — AI-Driven Voice/SMS Support Agent

An event-driven AI support agent that integrates **Aircall** (voice/SMS conversations),
**Jobber** (home-service business context), **Qdrant** (vector search with hybrid RRF),
and **OpenRouter** (free-tier LLMs) to provide automated, context-aware customer responses.

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Start infrastructure
docker-compose up -d postgres qdrant redis

# 3. Index documentation
uv run texponse-index

# 4. Start the API server
uv run texponse

# 5. Start the Celery worker (in a separate terminal)
uv run texponse-worker
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full system architecture,
[data flow diagram](docs/architecture.md#data-flow-summary), and
[scalability considerations](docs/architecture.md#scalability-considerations).

## Technology Stack

| Layer       | Choice                     |
|-------------|----------------------------|
| Language    | Python 3.11+ (async-first) |
| Framework   | FastAPI                    |
| Database    | PostgreSQL 15 (SQLModel)   |
| Vector DB   | Qdrant (hybrid search)   |
| Background  | Celery + Redis           |
| LLM         | OpenRouter (OpenAI SDK)  |
| Embedding   | OpenRouter /embeddings  |

See [docs/tech-stack.md](docs/tech-stack.md) for the full rationale.

## API Endpoints

- `POST /webhooks/aircall` — Aircall webhook receiver (signature-verified)
- `POST /responses/generate` — Manually trigger response generation
- `GET /health` — Health check

## Development

```bash
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy src/         # type-check
uv run pytest            # tests
```
