#!/usr/bin/env bash
# Smoke test a built image of the extraction API: does the packaged app actually work, not just the code?
#
#     deploy/smoke_test.sh <image> [expected-version]
#
# Runs in CI on every pull request (irs-990-extraction-image). Needs no API keys and spends nothing: it
# checks everything short of calling Claude.
#
#   1. Inside the image: the app runs as a non-root user, the prompts load (with the fingerprints their
#      manifests record), and no data/ came along (data/raw holds hundreds of MB of returns).
#   2. The server starts and /health reports ok, auth required, extraction disabled (no Anthropic key
#      in this test), and the expected version.
#   3. Auth and routing: /extract with no key -> 401; with the right key -> 503 (no Anthropic key),
#      which proves the request got past auth and the rate limit into the endpoint.
set -euo pipefail

IMAGE="${1:?usage: smoke_test.sh <image> [expected-version]}"
EXPECTED_VERSION="${2:-}"
NAME="irs990-smoke-$$"
PORT=18001
KEY="smoke-test-key"

fail() {
  echo "SMOKE TEST FAILED: $*"
  echo "--- container logs ---"
  docker logs "$NAME" 2>&1 | tail -40 || true
  exit 1
}
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "1. Inside the image"
MSYS_NO_PATHCONV=1 docker run --rm --network none --entrypoint python "$IMAGE" -c "
import os, pathlib, sys
sys.path.insert(0, 'src')
import prompts
assert os.getuid() != 0, 'running as root'
for name in ('extract', 'extract_retry'):
    p = prompts.load(name)
    assert p.sha256 == prompts.manifest(name)['versions'][p.version]['sha256'], name
    print('   prompt', p.id, 'ok')
assert not pathlib.Path('/app/data').exists(), 'data/ is in the image'
print('   non-root user, no data/')
" || { echo "SMOKE TEST FAILED: inside the image"; exit 1; }

echo "2. Server starts and reports healthy"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" -e IRS990_API_KEY="$KEY" "$IMAGE" >/dev/null
health=""
for _ in $(seq 1 60); do
  health=$(curl -sf "http://127.0.0.1:$PORT/health" || true)
  [ -n "$health" ] && break
  # Don't wait out the full minute if the app has already crashed.
  [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ] || fail "the container exited during startup"
  sleep 1
done
[ -n "$health" ] || fail "/health did not respond within 60 s"
echo "   $health"
echo "$health" | grep -q '"status":"ok"' || fail "/health status is not ok"
echo "$health" | grep -q '"auth_required":true' || fail "auth is not enforced"
echo "$health" | grep -q '"extraction_enabled":false' || fail "extraction should be off without an Anthropic key"
if [ -n "$EXPECTED_VERSION" ]; then
  echo "$health" | grep -q "\"version\":\"$EXPECTED_VERSION\"" || fail "version is not $EXPECTED_VERSION"
fi

echo "3. Auth and routing"
# The upload goes in through curl's standard input (@-): a temporary file's path isn't portable to
# Windows' curl.exe, and the endpoint never gets as far as reading it anyway.
pdf() { printf '%%PDF-1.4\n'; }
code=$(pdf | curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/extract" \
  -F "file=@-;filename=smoke.pdf;type=application/pdf")
[ "$code" = 401 ] || fail "/extract without a key returned $code, expected 401"
echo "   no key -> 401"
code=$(pdf | curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/extract" \
  -H "X-API-Key: $KEY" -F "file=@-;filename=smoke.pdf;type=application/pdf")
[ "$code" = 503 ] || fail "/extract with the key returned $code, expected 503 (no Anthropic key in this test)"
echo "   right key -> 503 (reached the endpoint; no Anthropic key configured here)"

echo "SMOKE TEST PASSED: $IMAGE"
