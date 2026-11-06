# The gateway's container image (step 8): what Kubernetes runs.
#
#     docker build -t ai-gateway:dev .
#
# Two stages. The first installs the locked production dependencies with uv; the second copies only the result
# onto a clean Python base, so the image carries no build tools, no uv, no tests and no dev packages. Both base
# images are pinned by digest: a rebuild gets exactly these, until they're updated on purpose.

FROM python:3.14.10-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /bin/uv
# Compile to bytecode now (faster start), copy files rather than link them (the venv moves to another stage), and use
# the base image's Python, never a downloaded one.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Exactly uv.lock's versions, checksums verified (--frozen fails instead of re-resolving); production packages only.
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.14.10-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
# A user with no login and no home: the process can't write to the code or the system.
RUN groupadd --system --gid 10001 gateway && useradd --system --uid 10001 --gid gateway --no-create-home gateway
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src/ src/
COPY config/ config/
COPY migrations/ migrations/
# The registered prompts (step 9), checked against their fingerprints when the gateway starts.
COPY prompts/ prompts/
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_METRICS_PORT=9464 \
    GATEWAY_METRICS_ADDR=0.0.0.0
USER 10001:10001
# The API, and the metrics (reachable only from inside the cluster: no Service publishes them to callers).
EXPOSE 8000 9464
# No access log: the gateway writes its own JSON line per request. On shutdown (SIGTERM, as Kubernetes sends) uvicorn
# stops accepting connections and gives requests in progress up to 30 s to finish.
CMD ["uvicorn", "app:create_app", "--factory", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--timeout-graceful-shutdown", "30"]
