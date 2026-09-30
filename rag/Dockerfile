# --- Stage 1: install dependencies into a virtualenv ---
FROM python:3.10-slim AS builder

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt .
# CPU-only torch first: the default PyPI build bundles CUDA libraries (several GB) this image never uses.
# torch comes from PyTorch's CPU index; its dependencies still come from PyPI.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \
 && pip install --no-cache-dir -r requirements.txt

# --- Stage 2: runtime image with only the finished virtualenv and the app ---
FROM python:3.10-slim

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
