"""A local stand-in for API Gateway and the Lambda functions behind it (step 8): the reviewer UI's dev server talks to
it with `npm run dev:api`. No AWS, no model calls.

    uv run python tests/local_api.py              # http://127.0.0.1:8000; the UI proxies /api here

At start the dev packets run through the stand-in steps into in-memory stores, as the UI's fixtures are made
(tests/test_ui_contract.seed). Each request becomes the event API Gateway would send api.handler (HTTP API, payload
format 2.0); a decision resumes in-process through handlers.resume, as the Resume function would. Letters are
placeholders ("Letter for gp-027"). Everything is in memory: restarting starts over.

Sign-in: with the reviewers' user pool configured (ui/.env.api.local, as the UI reads it, or --user-pool-id and
--client-id), every request needs a real Cognito access token, checked as API Gateway's JWT authorizer will check it
(CognitoVerifier); without one it answers 401, as API Gateway does. Without a pool configured, every request is signed
in as a fixed local reviewer.

It lives with the tests because it's built from their stand-ins, and only for local use: it listens on 127.0.0.1.
"""

import argparse
import json
import pathlib
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

import jwt

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from test_ui_contract import seed, with_rubric  # noqa: E402
from test_workflow import right_answers  # noqa: E402

import api  # noqa: E402
import applications  # noqa: E402
import handlers  # noqa: E402
import workflow  # noqa: E402

PORT = 8000
UI_ENV = HERE.parent / "ui" / ".env.api.local"  # where `npm run dev:api` reads the pool's IDs (not committed)
REVIEWER = "local.reviewer@example.org"  # without a pool configured: a fixed reviewer
CLAIMS = {"username": REVIEWER, "cognito:groups": "[reviewers]"}  # as API Gateway passes them
UNAUTHORIZED = {"statusCode": 401, "body": '{"message":"Unauthorized"}'}  # API Gateway's own answer
ROUTES = [  # (method, path pattern, route key): API Gateway's routing, and its decoding of path parameters
    ("GET", re.compile(r"/applications"), "GET /applications"),
    ("GET", re.compile(r"/applications/(?P<application_id>[^/]+)"), "GET /applications/{application_id}"),
    (
        "POST",
        re.compile(r"/applications/(?P<application_id>[^/]+)/decision"),
        "POST /applications/{application_id}/decision",
    ),
]


def _as_text(value) -> str:
    """API Gateway passes every claim as text: a list as "[a b]", a number as its digits."""
    if isinstance(value, list):
        return "[" + " ".join(map(str, value)) + "]"
    return str(value)


class CognitoVerifier:
    """What API Gateway's JWT authorizer checks before our code runs: the token's signature against the pool's
    published keys, its issuer, its app client, that it's an access token, and that it hasn't expired. Groups are
    left to the API (api._reviewer), as the authorizer leaves them."""

    def __init__(self, user_pool_id: str, client_id: str, keys=None):
        region = user_pool_id.split("_", 1)[0]
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self.client_id = client_id
        self.keys = keys or jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json")  # fetched once, then cached

    def check(self, authorization: str | None) -> tuple[dict | None, str]:
        """The token's claims as API Gateway would pass them, or None and why it was refused (401). The reason is for
        the local log: never the token itself."""
        if not authorization:
            return None, "no Authorization header"
        if not authorization.startswith("Bearer "):
            return None, "not a Bearer token"
        token = authorization.removeprefix("Bearer ")
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"require": ["exp", "iss", "token_use", "client_id"], "verify_aud": False},
            )  # access tokens carry client_id, not aud
        except jwt.PyJWTError as e:
            return None, f"{type(e).__name__}: {e}"  # e.g. ExpiredSignatureError, InvalidSignatureError
        if claims["token_use"] != "access":
            return None, f"a {claims['token_use']} token, not an access token"
        if claims["client_id"] != self.client_id:
            return None, "issued to another app client"
        return {name: _as_text(value) for name, value in claims.items()}, "ok"

    def claims(self, authorization: str | None) -> dict | None:
        return self.check(authorization)[0]


def event(method: str, target: str, body: str | None, claims: dict) -> dict | None:
    """The HTTP API event for a request, or None when no route matches (API Gateway answers 404 itself)."""
    url = urlsplit(target)
    for route_method, pattern, route_key in ROUTES:
        match = pattern.fullmatch(url.path)
        if match and method == route_method:
            return {
                "routeKey": route_key,
                "pathParameters": {k: unquote(v) for k, v in match.groupdict().items()} or None,
                "queryStringParameters": dict(parse_qsl(url.query)) or None,
                "body": body,
                "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
            }
    return None


def respond(method: str, target: str, body: str | None, authorization: str | None, verifier) -> dict:
    """API Gateway's part: the authorizer first (when a pool is configured), then routing, then the handler."""
    claims, reason = verifier.check(authorization) if verifier else (CLAIMS, "ok")
    if claims is None:
        print(f"401 {method} {urlsplit(target).path}: {reason}", file=sys.stderr, flush=True)
        return UNAUTHORIZED
    request = event(method, target, body, claims)
    return api.handler(request) if request else {"statusCode": 404, "body": '{"message":"Not Found"}'}


LOCK = threading.Lock()  # connections on threads (a browser holds idle ones open), requests one at a time: the
# in-memory stores aren't thread-safe, and in Lambda each instance handles one request at a time too
VERIFIER: CognitoVerifier | None = None


class Handler(BaseHTTPRequestHandler):
    def _answer(self, method: str) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else None
        with LOCK:
            response = respond(method, self.path, body, self.headers.get("authorization"), VERIFIER)
        payload = response["body"].encode("utf-8")
        self.send_response(response["statusCode"])
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 (http.server's naming)
        self._answer("GET")

    def do_POST(self):  # noqa: N802
        self._answer("POST")


def ui_env(path: pathlib.Path) -> dict[str, str]:
    """KEY=VALUE lines of a Vite env file (comments and blank lines skipped)."""
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs if not k.strip().startswith("#")}


def main() -> int:
    global VERIFIER
    env = ui_env(UI_ENV)
    parser = argparse.ArgumentParser(description="The reviewer API on 127.0.0.1, with stand-in steps.")
    parser.add_argument("--user-pool-id", default=env.get("VITE_COGNITO_USER_POOL_ID"))
    parser.add_argument("--client-id", default=env.get("VITE_COGNITO_CLIENT_ID"))
    args = parser.parse_args()
    if bool(args.user_pool_id) != bool(args.client_id):
        parser.error("--user-pool-id and --client-id go together")
    VERIFIER = CognitoVerifier(args.user_pool_id, args.client_id) if args.user_pool_id else None

    checkpointer = workflow.memory_checkpointer()
    graph = workflow.build(with_rubric(right_answers()), checkpointer=checkpointer)
    store = applications.MemoryStore()
    api._workflow = lambda: workflow.reader(checkpointer)
    api._applications = lambda: store
    api._resume = handlers.resume
    handlers._workflow = lambda: graph
    handlers._applications = lambda: store
    with tempfile.TemporaryDirectory() as folder:
        seed(graph, store, pathlib.Path(folder))
        waiting = len(store.list("waiting_for_review"))
        who = f"Cognito tokens from {VERIFIER.issuer}" if VERIFIER else f"signed in as {REVIEWER}"
        print(json.dumps({"api": f"http://127.0.0.1:{PORT}", "waiting": waiting, "sign_in": who}))
        try:
            ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
