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

# Embedding and reranker models download into ~/.cache on first use; docker-compose mounts a
# volume there so they survive container restarts. The directory must exist and belong to
# appuser so the volume inherits that ownership.
RUN useradd --create-home appuser \
 && mkdir -p /home/appuser/.cache \
 && chown appuser /home/appuser/.cache

COPY src/ ./src/
COPY data/ ./data/

USER appuser

EXPOSE 8000

CMD ["uvicorn", "api:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
