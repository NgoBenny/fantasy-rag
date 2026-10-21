# The API (FastAPI + uvicorn). Build from the repo root:
#   docker compose up -d db api            (live, reads .env)
#   docker compose --profile demo up api-demo   (sample data: no .env, database or key)
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/ ./
# Demo fixtures only; .env, caches and logs are excluded by .dockerignore.
COPY data/demo /app/data/demo
RUN uv sync --locked --no-dev
ENV PATH=/app/backend/.venv/bin:$PATH
EXPOSE 8010
CMD ["uvicorn", "fantasy_rag.api.main:app", "--host", "0.0.0.0", "--port", "8010"]
