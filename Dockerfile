FROM python:3.11-slim AS base

WORKDIR /app

RUN pip install --no-cache-dir uv==0.12.17

COPY pyproject.toml uv.lock* ./

RUN uv export --frozen --no-hashes --no-emit-project -o requirements.txt --all-extras || \
    uv export --frozen --no-emit-project -o requirements.txt || \
    pip freeze > requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS runtime

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src

COPY src/ ./src/
COPY alembic.ini ./
COPY src/texponse/db/migrations ./migrations

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)"

CMD ["uvicorn", "texponse.main:app", "--host", "0.0.0.0", "--port", "8000"]
