# --- Stage 1: install the locked dependencies into a virtualenv ---
FROM python:3.14-slim AS builder

# uv installs exactly what uv.lock specifies, verifying every package's checksum. Pinned to a version
# so the build doesn't change when uv releases.
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv

# UV_PROJECT_ENVIRONMENT: install into /opt/venv, which stage 2 copies.
# UV_PYTHON_DOWNLOADS=never: use this image's Python, never download another.
# UV_COMPILE_BYTECODE=1: pre-compile .py files now, so the container doesn't on every cold start.
# UV_LINK_MODE=copy: real file copies (the default hard links don't survive the stage-to-stage copy).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv     UV_PYTHON_DOWNLOADS=never     UV_COMPILE_BYTECODE=1     UV_LINK_MODE=copy

WORKDIR /build
COPY pyproject.toml uv.lock ./
# --frozen: install uv.lock as-is and fail if it's out of date, rather than re-resolving.
# --no-dev: leave out test and audit tools.
RUN uv sync --frozen --no-dev

# --- Stage 2: runtime image with only the finished virtualenv and the app ---
# Same Python image as stage 1: the virtualenv points at this exact interpreter.
FROM python:3.14-slim

COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Embedding and reranker models download into ~/.cache; docker-compose mounts a volume there so they
# survive container restarts. The directory must exist and belong to appuser so the volume inherits
# that ownership.
RUN useradd --create-home appuser \
 && mkdir -p /home/appuser/.cache /app/chroma_db \
 && chown appuser /home/appuser/.cache /app/chroma_db

USER appuser

# Steps are ordered from least to most often changed, because Docker re-runs every step after the
# first one whose inputs changed. Models first: this step only depends on two small files that rarely
# change, so it stays cached across ordinary code changes. It downloads the embedding model and the
# reranker (~90 MB), so containers never fetch them from the internet at runtime.
COPY src/model_names.py src/download_models.py ./src/
RUN python src/download_models.py

COPY src/ ./src/
COPY data/ ./data/
COPY prompts/ ./prompts/

# Build the search index into the image, so a deployed container needs no database server: the
# corpus is small and static, and changing it means rebuilding the image anyway. docker-compose
# ignores this built-in index, because it sets CHROMA_HOST and uses its Chroma server instead.
RUN python src/ingest.py --docs-dir data/current_events

# The commit this image was built from, reported by /health so a deploy can confirm which version is
# live. Last on purpose: it changes on every commit, and everything after it would be rebuilt too.
ARG GIT_SHA=dev
ENV RAG_VERSION=$GIT_SHA

EXPOSE 8000

CMD ["uvicorn", "api:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
