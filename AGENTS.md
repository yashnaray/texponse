# AGENTS.md — Texponse Development Guide

## Project Overview
Texponse is an AI-driven voice/SMS support agent that integrates **Aircall** (conversations),
**Jobber** (business context), **Qdrant** (vector retrieval), and **OpenRouter** (free-tier LLMs)
to provide automated, context-aware support responses.

## Language & Frameworks
- **Language:** Python 3.11+ (async-first)
- **Web framework:** FastAPI (async, OpenAPI-first)
- **Database:** PostgreSQL 15 (SQLModel / SQLAlchemy 2 async engine)
- **Vector DB:** Qdrant (REST API)
- **Background jobs:** Celery + Redis
- **HTTP client:** httpx (async)
- **LLM client:** OpenAI Python SDK pointed at OpenRouter
- **Lint/format:** ruff, mypy, black

## Project Structure
```
src/texponse/
  config.py          # Pydantic-settings, environment validation
  main.py            # FastAPI app factory
  api/               # HTTP routes (webhooks, response endpoints)
  integrations/      # Aircall, Jobber, OpenRouter client wrappers
  rag/               # Retriever (Qdrant), embedder, prompt builder
  db/                # SQLModel models + repositories
  security/          # Guardrails, RBAC, webhook signature verification
  tasks/             # Celery tasks (indexing, response generation)
```

## Conventions
- All HTTP calls must use `httpx.AsyncClient` (no `requests` in async code).
- Database access goes through repository classes — never touch SQLModel directly in routes.
- Secrets read from env via `Settings()` — never hardcode.
- Every public function that hits an external API must declare its timeout.
- Background work (LLM calls, indexing) MUST run in Celery, never inline in a request handler.
- Type hints required on all public functions; `python3.11` syntax (`X | Y`, `list[...]`).

## Commands
```bash
uv sync                # install deps
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy src/       # type-check
uv run pytest          # test
docker-compose up -d   # dev infra (postgres, qdrant, redis)
```
