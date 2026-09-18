# Implementation Roadmap

## Phase 0: Project Scaffolding (Days 1–2)

**Goals:** Reproducible dev environment, linting, type-checking, CI pipeline.

| Task | Details |
|------|---------|
| Initialize `uv` project | `pyproject.toml` with all dependencies from `docs/tech-stack.md` |
| Docker Compose infra | Services: postgres, qdrant, redis, prometheus, grafana |
| Dockerfile | Multi-stage build: `uv export` → install → copy |
| Alembic migrations | Base `alembic.ini` + env.py with async engine |
| CI config | GitHub Actions: lint → type-check → test → build |
| AGENTS.md | This file + coding conventions |
| Project skeleton | `src/texponse/` with `__init__.py` sub-packages |

**Deliverables:** `uv run ruff check .`, `uv run mypy src/`, `docker compose up` all pass.

## Phase 1: Data Ingestion Layer (Days 3–5)

**Goals:** Webhook listener that ingests, verifies, and normalises Aircall events.

| Task | Details |
|------|---------|
| Config schema | `config.py` — Pydantic Settings for all env vars (Aircall token, Jobber client ID/secret, OpenRouter key, DB URL, Redis URL, Qdrant URL, secret key) |
| Webhook signature verification | HMAC-SHA256 verification of `X-Arcall-Signature` header |
| Aircall client | Thin `httpx.AsyncClient` wrapper for Aircall REST API (`/v1/calls`, `/v1/messages`, `/v1/contacts`, `/v1/webhooks`) |
| DB models | SQLModel models for `ConversationEvent`, `Conversation`, `Users`, `Roles` |
| Repositories | `ConversationRepository`, `UserRepository` — all queries go through repos |
| Webhook endpoint | `POST /webhooks/aircall` — verify, normalise, persist, enqueue Celery task |
| Rate limiting | `slowapi` decorator at 60 req/min per IP |

**Data flow:**
```
Aircall webhook
  → POST /webhooks/aircall
  → verify_signature() → rejects 401 if invalid
  → normalize_event() → ConversationEvent model
  → ConversationRepository.save_event()
  → celery.send_task("texponse.tasks.generate_response", ...)
  → 200 OK (immediately)
```

## Phase 2: Embedding Pipeline & Qdrant Index (Days 6–8)

**Goals:** Index technical documentation into Qdrant with hybrid (dense + BM25) vectors.

| Task | Details |
|------|---------|
| OpenRouter embedder | `embedder.py` — calls `/embeddings` endpoint (model: `openai/text-embedding-3-small`) |
| BM25 tokenizer | Use `qdrant-client[fastembed]` `BM25Tokenizer` or Qdrant's built-in sparse text index |
| Indexing task | `tasks/indexer.py` — chunk → embed → upsert to Qdrant with dense + sparse vectors |
| Collection schema | `dense` (768-dim, Cosine) + `bm25` (sparse, idf modifier) |
| Metadata | Store `source_url`, `category`, `last_updated` in payload for filtering |
| Sync CLI | `uv run texponse index-docs --source docs/` |

## Phase 3: Hybrid RAG Retriever (Days 9–11)

**Goals:** Hybrid search combining vector similarity + BM25 with RRF fusion.

| Task | Details |
|------|---------|
| Retriever class | `rag/retriever.py` — builds both dense and sparse query vectors |
| RRF fusion | Qdrant native `search` with `search_type: "hybrid"` + `fusion: "rrf"` (k=60) |
| Fallback logic | If dense query vector generation fails, fall back to sparse-only search |
| Result deduplication | Merge by `point_id`, keep highest score |
| Context assembly | Sort by score, truncate to `max_tokens` budget |
| Integration | Retriever called from `celery/tasks.generate_response` |

## Phase 4: Jobber Integration (Days 12–14)

**Goals:** Contextual business data enrichment from Jobber's GraphQL API.

| Task | Details |
|------|---------|
| Jobber OAuth client | `integrations/jobber.py` — OAuth token exchange + refresh rotation |
| GraphQL client | `httpx.AsyncClient` POST to `https://api.getjobber.com/api/graphql` |
| Token storage | `JobberTokenRepository` — encrypted at rest in `jobber_tokens` table |
| Cache layer | Redis TTL cache (10 min) for client/job/invoice lookups |
| Query templates | Predefined GraphQL queries for `clientByPhone`, `jobsForClient`, `propertiesForClient` |
| Enrichment task | `tasks/sync_jobber_data.py` — periodic sync for warm cache |

## Phase 5: LLM Orchestration (Days 15–18)

**Goals:** Prompt assembly with context-window management, model selection, and retry/fallback.

| Task | Details |
|------|---------|
| Prompt builder | `rag/prompt_builder.py` — assembles system prompt + context + history |
| System prompt | Guardrails (Section 6 of architecture.md) |
| Context budgeting | Token counting via `tiktoken`; truncate history + passages to fit window |
| Model config | Per-model context-window limits + token budgets (hardcoded table) |
| OpenRouter client | `integrations/openrouter.py` — `chat.completions.create()` wrapper |
| Retry logic | 3 retries with exponential backoff; 529/429 triggers fallback model |
| Circuit breaker | Track failures; degrade to "safe response" after 5 consecutive failures |
| Streaming support | `stream=True` for long responses; SSE endpoint for real-time UI |

## Phase 6: Response Dispatch (Days 19–20)

**Goals:** Send the LLM-generated response back to the customer via Aircall.

| Task | Details |
|------|---------|
| Response model | `Response` SQLModel — stores input prompt, output text, model used, token usage |
| Aircall send-message | `POST /v1/messages` with `to`, `from`, `content` |
| PII redaction | Scan output for phone/email patterns before sending |
| Response storage | Persist to `responses` table for audit trail |

## Phase 7: Security & Guardrails (Days 21–22)

**Goals:** Comprehensive security hardening.

| Task | Details |
|------|---------|
| Webhook validation | HMAC signature check (already in Phase 1) |
| Input sanitisation | Strip HTML, reject oversized payloads, detect injection patterns |
| Prompt injection detection | Regex + keyword blocklist for known injection patterns |
| Output PII scanning | Regex for phone numbers, emails, credit cards → redact |
| Toxicity / profanity filter | Blocklist of refusal-trigger words |
| RBAC enforcement | Decorator-based: `@require_role("admin")` / `@require_role("agent")` |
| TLS everywhere | All external traffic via HTTPS; internal Service-Mesh planned |
| Secret rotation | Cron task to rotate tokens (Celery beat) |

## Phase 8: Observability & Testing (Days 23–25)

**Goals:** Production-ready observability + comprehensive test suite.

| Task | Details |
|------|---------|
| OpenTelemetry | Tracing spans for webhook → worker → RAG → LLM → dispatch |
| Prometheus metrics | `texponse_webhook_duration_seconds`, `texponse_llm_tokens_total`, `texponse_rag_results` |
| Structured logging | JSON logs with `correlation_id`, `conversation_id`, `company_id` |
| Unit tests | `pytest` for each module (config, normaliser, embedder, retriever, prompt builder) |
| Integration tests | Testcontainers for Postgres + Qdrant; end-to-end webhook → response |
| Mock strategies | `respx` for httpx mocking; `fakeredis` for Redis; in-memory SQLite for DB |
| CI test matrix | Python 3.11 + 3.12, lint + typcheck + test gates |

## Phase 9: Deployment (Days 26–28)

**Goals:** Production deployment to Kubernetes.

| Task | Details |
|------|---------|
| Helm chart | `helm/` directory with app + worker + infra templates |
| Ingress | NGINX ingress with TLS termination |
| Secret management | Sealed secrets or external Secrets Operator |
| Health checks | Liveness/readiness probes on `/health` |
| Horizontal autoscaling | HPA based on CPU (webhook) + queue length (workers) |
| Blue-green deploy | K8s rolling update strategy |

## Milestone: MVP (End of Phase 5)

A minimal end-to-end flow:
1. Aircall webhook received → verified → stored.
2. Conversation context retrieved from PostgreSQL.
3. Qdrant hybrid search returns relevant documentation.
4. Prompt assembled → OpenRouter LLM generates response.
5. Response sent back to Aircall via `sendMessage`.

**Success criteria:** < 15s end-to-end latency (including LLM), > 95% webhook delivery success.
