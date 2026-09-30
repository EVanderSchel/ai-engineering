#!/usr/bin/env bash
# Smoke test a built rag-api image: does the packaged app actually work, not just the code?
#
#     deploy/smoke_test.sh <image> [expected-version]
#
# Runs in CI on every pull request (rag-image) and on the exact image rag-publish is about to push.
# Needs no API keys and spends nothing: it checks everything short of calling Claude.
#
#   1. Offline search: with networking disabled, hybrid search + reranking must return the expected
#      document. Proves the models and the search index are inside the image.
#   2. The server starts and /health reports ok, auth required, and the expected version.
#   3. Auth and routing: /ask with no key -> 401; with the right key -> 503 (no Anthropic key is
#      configured in this test), which proves the request got past auth into the endpoint.
set -euo pipefail

IMAGE="${1:?usage: smoke_test.sh <image> [expected-version]}"
EXPECTED_VERSION="${2:-}"
NAME="rag-smoke-$$"
PORT=18000
KEY="smoke-test-key"

fail() {
  echo "SMOKE TEST FAILED: $*"
  echo "--- container logs ---"
  docker logs "$NAME" 2>&1 | tail -40 || true
  exit 1
}
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "1. Offline search inside the image (no network)"
MSYS_NO_PATHCONV=1 docker run --rm --network none -e HF_HUB_OFFLINE=1 --entrypoint python "$IMAGE" -c "
import sys
sys.path.insert(0, 'src')
from retrieval import retrieve
hits = retrieve('What ship was struck in the Strait of Hormuz?', hybrid=True, use_reranker=True)
sources = [h['source'] for h in hits]
print('   top sources:', sources)
assert sources and sources[0] == 'armed_conflicts.txt', f'unexpected top source: {sources}'
" || { echo "SMOKE TEST FAILED: offline search"; exit 1; }

echo "2. Server starts and reports healthy"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" -e RAG_API_KEY="$KEY" "$IMAGE" >/dev/null
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
if [ -n "$EXPECTED_VERSION" ]; then
  echo "$health" | grep -q "\"version\":\"$EXPECTED_VERSION\"" || fail "version is not $EXPECTED_VERSION"
fi

echo "3. Auth and routing"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/ask" \
  -H 'Content-Type: application/json' -d '{"question": "smoke test"}')
[ "$code" = 401 ] || fail "/ask without a key returned $code, expected 401"
echo "   no key -> 401"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/ask" \
  -H 'Content-Type: application/json' -H "X-API-Key: $KEY" -d '{"question": "smoke test"}')
[ "$code" = 503 ] || fail "/ask with the key returned $code, expected 503 (no Anthropic key in this test)"
echo "   right key -> 503 (reached the endpoint; no Anthropic key configured here)"

echo "SMOKE TEST PASSED: $IMAGE"
