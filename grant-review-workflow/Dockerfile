# The workflow as a Lambda container image (step 7). Built and pushed by CDK (infra/); run locally with Docker:
#
#   docker build -t grant-review-workflow .
#   docker run --rm -p 9000:8080 grant-review-workflow                  # Lambda's runtime emulator, handler "start"
#   curl -X POST http://127.0.0.1:9000/2015-03-31/functions/function/invocations -d '{...event...}'
#
# (This emulator version serves the 2015-03-31 path; guides showing 2015-03-25 get a 404.) Add "handlers.resume"
# after the image name to run the resume handler instead.
#
# A container image rather than a zip: pymupdf and LangGraph together are close to the zip size limit, and the
# same image runs locally, in tests and in Lambda. x86_64 (the build machine's architecture).

# AWS's Python 3.14 Lambda base image, pinned by digest so a rebuild gets the same OS and runtime.
FROM public.ecr.aws/lambda/python:3.14@sha256:252cbb0879717b1c049b9f909d86d06d88e129f3208386d7ba1189d535f5f981

# Dependencies first (a separate layer, rebuilt only when the lock changes): exactly the versions in uv.lock,
# with their hashes checked, runtime dependencies only. uv is used only at build time and isn't in the image.
COPY pyproject.toml uv.lock /build/
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.21,source=/uv,target=/bin/uv \
    uv export --project /build --frozen --no-dev --no-emit-project --output-file /build/requirements.txt \
    && pip install --no-cache-dir --require-hashes --requirement /build/requirements.txt --target "${LAMBDA_TASK_ROOT}" \
    && rm -rf /build

# The workflow's code and prompts; paths.ROOT resolves to the task root, so prompts/ is found as in the repo.
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY prompts/ ${LAMBDA_TASK_ROOT}/prompts/

ENV PYTHONPATH=${LAMBDA_TASK_ROOT}/src \
    GRANT_REVIEW_CACHE_DIR=/tmp/propublica-cache \
    GRANT_REVIEW_WORK_DIR=/tmp/applications

# Each function picks its handler (CDK sets handlers.start or handlers.resume); start is the default.
CMD ["handlers.start"]
