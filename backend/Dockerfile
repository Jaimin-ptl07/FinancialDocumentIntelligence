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

# Phase 1.5: bake the BGE embedding weights. Constructing TextEmbedding is
# what downloads the model (see embeddings.py's laziness note); the one-word
# warmup embed additionally loads the ONNX session, so a truncated download
# fails the BUILD instead of the first pipeline run. FASTEMBED_CACHE_PATH is
# fastembed's own env seam (define_cache_dir) -- the runtime stage sets the
# same var, so the first embed in a fresh container resolves the baked copy
# instead of re-downloading ~130 MB per recreation.
# Sits between the two sync phases deliberately: its only inputs are the
# lockfile-frozen venv and the ARG, so source edits below don't invalidate the
# 130 MB layer. Compose's x-backend-build passes FDI_EMBED_MODEL from the same
# .env the runtime reads, so the baked model tracks the configured one; this
# default only serves a bare `docker build` and must match
# Settings.embed_model's default.
ARG FDI_EMBED_MODEL=BAAI/bge-small-en-v1.5
ENV FASTEMBED_CACHE_PATH=/opt/fdi/fastembed-cache
RUN /app/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='${FDI_EMBED_MODEL}').embed(['warmup'])"

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
COPY --chown=fdi:fdi docs/architecture/field-extraction-schema.json /app/docs/architecture/field-extraction-schema.json
# The baked embedding weights, at the path the builder's FASTEMBED_CACHE_PATH
# downloaded them to. chown'd, not left root-owned: huggingface_hub resolves
# the snapshot on every construction and rewrites ref/lock files in-place, so
# the cache must stay writable by fdi.
COPY --from=builder --chown=fdi:fdi /opt/fdi/fastembed-cache /opt/fdi/fastembed-cache

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/fdi/fastembed-cache

WORKDIR /app
USER fdi
EXPOSE 8000

CMD ["uvicorn", "fdi.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
