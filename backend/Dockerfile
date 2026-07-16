# syntax=docker/dockerfile:1
########################  builder  ########################
FROM python:3.13-slim-trixie AS builder

# Distroless uv image — the ONLY correct kind for COPY --from.
# Pinned, not :latest — the uv guide calls pinning best practice for reproducibility.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Phase 1: dependencies only. Cached until uv.lock / pyproject.toml change.
# --locked fails the build on a stale lockfile (--frozen is for workspaces).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=backend/uv.lock,target=uv.lock \
    --mount=type=bind,source=backend/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --no-editable

# Phase 2: the project itself.
COPY backend/ /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

########################  runtime  ########################
FROM python:3.13-slim-trixie

# No curl, and no apt layer at all. The compose healthcheck hits /health with the
# venv's own Python (`python -c "import urllib.request..."`) -- zero added bytes
# versus curl's ~13.5 MB, and no dependency on the tool that a stale
# CURL_CA_BUNDLE breaks host-side. See the healthcheck in Task 20's compose.
RUN groupadd --system fdi && useradd --system --gid fdi --home-dir /app fdi

COPY --from=builder --chown=fdi:fdi /app/.venv /app/.venv
COPY --from=builder --chown=fdi:fdi /app/alembic /app/alembic
COPY --from=builder --chown=fdi:fdi /app/alembic.ini /app/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER fdi
EXPOSE 8000

CMD ["uvicorn", "fdi.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
