# syntax=docker/dockerfile:1.7
# One image, three roles: API (uvicorn), worker (celery), migrate (python -m app.cli bootstrap).
# The React SPA is built in stage 1 and served by FastAPI, so prod has a single origin (no CORS, no build-time API URL).

# ---- Stage 1: build the React dashboard -------------------------------------------------------------
FROM node:24-alpine AS web
WORKDIR /web
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ---- Stage 2: Python app ------------------------------------------------------------------------------
FROM python:3.13-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
# The venv lives outside /srv so a dev bind-mount of ./app can never shadow it.
# UV_HTTP_TIMEOUT: with uv's default (30 s) a cold build failed twice on a slow link while downloading packages.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=120 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /srv

# Dependencies first (cached layer), then source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts
COPY evals ./evals
# The SPA lives outside ./app on purpose: the dev stack bind-mounts ./app over /srv/app, which used to hide the build,
# so `make up` served a JSON 404 at :8000. app/main.py serves /srv/spa when it exists (SPA_DIR overrides).
COPY --from=web /web/dist /srv/spa

RUN useradd --create-home --uid 10001 vault && chown -R vault /srv
USER vault
EXPOSE 8000
# Render injects $PORT; locally it defaults to 8000. `sh -c` is needed so $PORT expands.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
