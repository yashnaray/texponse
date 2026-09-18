FROM python:3.11-slim AS base

WORKDIR /app

RUN pip install --no-cache-dir uv==4.1.0

COPY pyproject.toml uv.lock* ./

RUN uv export --frozen --no-hashes -o requirements.txt --all-extras 2>/dev/null || \
    uv export --frozen -o requirements.txt 2>/dev/null || \
    pip freeze > requirements.txt; \
    uv pip install --no-cache -r requirements.txt

FROM base AS runtime

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV UV_CACHE_DIR=/tmp/uv-cache

COPY src/ ./src/
COPY docs/ ./docs/

EXPOSE 8000 8001

ENTRYPOINT ["uv", "run"]
