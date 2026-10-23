"""A local stand-in for API Gateway and the Lambda functions behind it (step 8): the reviewer UI's dev server talks to
it with `npm run dev:api`. No AWS, no model calls.

    uv run python tests/local_api.py              # http://127.0.0.1:8000; the UI proxies /api here

At start the dev packets run through the stand-in steps into in-memory stores, as the UI's fixtures are made
(tests/test_ui_contract.seed). Each request becomes the event API Gateway would send api.handler (HTTP API, payload
format 2.0), signed in as a fixed local reviewer; a decision resumes in-process through handlers.resume, as the Resume
function would. Letters are placeholders ("Letter for gp-027"). Everything is in memory: restarting starts over.

It lives with the tests because it's built from their stand-ins, and only for local use: it listens on 127.0.0.1.
"""

import pathlib
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from test_ui_contract import seed, with_rubric  # noqa: E402
from test_workflow import right_answers  # noqa: E402

import api  # noqa: E402
import applications  # noqa: E402
import handlers  # noqa: E402
import workflow  # noqa: E402

PORT = 8000
REVIEWER = "local.reviewer@example.org"  # what stage 4's sign-in will supply
ROUTES = [  # (method, path pattern, route key): API Gateway's routing, and its decoding of path parameters
    ("GET", re.compile(r"/applications"), "GET /applications"),
    ("GET", re.compile(r"/applications/(?P<application_id>[^/]+)"), "GET /applications/{application_id}"),
    (
        "POST",
        re.compile(r"/applications/(?P<application_id>[^/]+)/decision"),
        "POST /applications/{application_id}/decision",
    ),
]


def event(method: str, target: str, body: str | None) -> dict | None:
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
                "requestContext": {"authorizer": {"jwt": {"claims": {"email": REVIEWER}}}},
            }
    return None


LOCK = threading.Lock()  # connections on threads (a browser holds idle ones open), requests one at a time: the
# in-memory stores aren't thread-safe, and in Lambda each instance handles one request at a time too


class Handler(BaseHTTPRequestHandler):
    def _answer(self, method: str) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else None
        request = event(method, self.path, body)
        with LOCK:
            response = api.handler(request) if request else {"statusCode": 404, "body": '{"message":"Not Found"}'}
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


def main() -> int:
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
        print(f"Local reviewer API on http://127.0.0.1:{PORT}: {waiting} waiting, signed in as {REVIEWER}")
        try:
            ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
