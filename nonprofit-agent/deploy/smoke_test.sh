#!/usr/bin/env bash
# Smoke test a built image of the agent API: does the packaged app actually work, not just the code?
#
#     deploy/smoke_test.sh <image> [expected-version]
#
# Runs in CI on every pull request (nonprofit-agent-image). Needs no API keys and spends nothing: it checks
# everything short of calling Claude.
#
#   1. Inside the image: the app runs as a non-root user, the agent and its tools import, the cache folder
#      is writable, and no eval data or saved runs came along.
#   2. The server starts and /health reports ok, auth required, answering disabled (no Anthropic key in
#      this test), the $2.00 daily cap, and the expected version.
#   3. Auth, validation and routing: /ask with no key -> 401; with the key but a too-long question -> 422;
#      with the key and a valid question -> 503 (no Anthropic key), which proves the request got past auth,
#      validation and the rate limit into the endpoint.
set -euo pipefail

IMAGE="${1:?usage: smoke_test.sh <image> [expected-version]}"
EXPECTED_VERSION="${2:-}"
NAME="nonprofit-agent-smoke-$$"
PORT=18002
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
import agent, api, paths
assert os.getuid() != 0, 'running as root'
assert len(agent.TOOL_SPECS) == 4, 'tools missing'
probe = paths.CACHE_DIR / 'write-test'
probe.write_text('ok'); probe.unlink()
assert not pathlib.Path('/app/evals').exists(), 'evals/ is in the image'
assert not any(pathlib.Path('/app/data').glob('**/*.json')), 'data files are in the image'
print('   non-root user, agent and 4 tools import, cache writable, no eval data or saved runs')
" || { echo "SMOKE TEST FAILED: inside the image"; exit 1; }

echo "2. Server starts and reports healthy"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" -e NONPROFIT_API_KEY="$KEY" "$IMAGE" >/dev/null
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
echo "$health" | grep -q '"answering_enabled":false' || fail "answering should be off without an Anthropic key"
echo "$health" | grep -q '"daily_cap_usd":2.0' || fail "the daily spend cap is not the expected \$2.00"
if [ -n "$EXPECTED_VERSION" ]; then
  echo "$health" | grep -q "\"version\":\"$EXPECTED_VERSION\"" || fail "version is not $EXPECTED_VERSION"
fi

echo "3. Auth, validation and routing"
ask() {  # ask <key or empty> <question>; prints the HTTP status
  local headers=(-H "Content-Type: application/json")
  [ -n "$1" ] && headers+=(-H "X-API-Key: $1")
  curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/ask" "${headers[@]}" \
    -d "{\"question\": \"$2\"}"
}
code=$(ask "" "What was Iowa Public Radio's revenue in 2023?")
[ "$code" = 401 ] || fail "/ask without a key returned $code, expected 401"
echo "   no key -> 401"
code=$(ask "$KEY" "$(printf 'x%.0s' $(seq 1 501))")
[ "$code" = 422 ] || fail "/ask with a 501-character question returned $code, expected 422"
echo "   too-long question -> 422"
code=$(ask "$KEY" "What was Iowa Public Radio's revenue in 2023?")
[ "$code" = 503 ] || fail "/ask with the key returned $code, expected 503 (no Anthropic key in this test)"
echo "   right key -> 503 (reached the endpoint; no Anthropic key configured here)"

echo "SMOKE TEST PASSED: $IMAGE"
