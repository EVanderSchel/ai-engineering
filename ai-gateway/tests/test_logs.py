"""The log line per request (logs.py): one JSON line, written when the response is done, with the request id the
caller saw and what happened, but never the messages or the answer."""

import json
import logging
from decimal import Decimal

import pytest
from conftest import AUTH, TEST_KEY, no_wait_policy, routes
from fastapi.testclient import TestClient

import app as gateway
import keys
import logs
from providers import ProviderError
from stub_provider import StubProvider

SECRET = "my card is 4111 1111 1111 1111"  # stands in for content that must never reach a log


@pytest.fixture
def lines(caplog):
    """The request lines logged so far, as the dicts the JSON formatter would write."""
    caplog.set_level(logging.INFO, logger="gateway")

    def read() -> list[dict]:
        out = []
        for record in caplog.records:
            if record.name == "gateway" and record.getMessage() == "request":
                out.append(json.loads(logs.JsonFormatter().format(record)))
        return out

    return read


def chat(client, **overrides):
    body = {"model": "stub", "max_tokens": 50, "messages": [{"role": "user", "content": SECRET}]}
    return client.post("/v1/chat/completions", json=body | overrides, headers=AUTH)


def test_a_completion_gets_one_line_with_what_happened_and_no_content(client, lines):
    response = chat(client)
    [line] = lines()
    assert line["request_id"] == response.headers["x-request-id"]
    assert line["level"] == "info" and line["ts"].endswith("Z")
    assert {k: line[k] for k in ("method", "path", "status", "team", "route", "stream", "outcome", "provider")} == {
        "method": "POST", "path": "/v1/chat/completions", "status": 200, "team": "test", "route": "stub",
        "stream": False, "outcome": "ok", "provider": "stub",
    }  # fmt: skip
    assert (line["model"], line["input_tokens"]) == ("stub-echo", 7)  # the stub counts words
    assert line["output_tokens"] == response.json()["usage"]["completion_tokens"]
    [attempt] = line["attempts"]
    assert {k: attempt[k] for k in ("provider", "model", "outcome")} == {
        "provider": "stub",
        "model": "stub-echo",
        "outcome": "ok",
    }
    assert line["fallback"] is False and line["duration_ms"] >= 0
    assert "cost_usd" not in line  # no price list without a database: left out, not written as null
    text = json.dumps(line)
    assert "4111" not in text and "Stub reply" not in text  # neither the question nor the answer


def test_a_stream_is_logged_after_its_last_event_with_the_first_token_time(client, lines):
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "stub", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi there"}],
    }) as response:  # fmt: skip
        assert lines() == []  # headers are out, the body isn't: nothing logged yet
        body = response.read().decode()
    [line] = lines()
    assert "[DONE]" in body
    assert line["stream"] is True and line["outcome"] == "ok" and line["status"] == 200
    assert 0 <= line["first_token_ms"] <= line["duration_ms"]


def test_refused_requests_are_logged_with_their_error_code(client, lines):
    no_key = {"model": "stub", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    client.post("/v1/chat/completions", json=no_key)
    chat(client, model="no-such-route")
    chat(client, temperature="hot")
    unauthorized, unknown, invalid = lines()
    assert (unauthorized["status"], unauthorized["error"], "team" in unauthorized) == (401, "invalid_api_key", False)
    assert (unknown["status"], unknown["error"], unknown["team"], unknown["route"]) == (
        404, "model_not_found", "test", "no-such-route",
    )  # fmt: skip
    assert (invalid["status"], invalid["error"]) == (400, "invalid_request")
    assert "outcome" not in unknown  # never admitted, so there's no hold to settle


def test_every_failed_attempt_is_in_the_line():
    down = StubProvider(fail=ProviderError("unavailable", "stub", "down"))
    app = gateway.create_app(
        routes(), {"stub": down, "anthropic": StubProvider("anthropic")}, {keys.key_hash(TEST_KEY): "test"},
        policy=no_wait_policy(),
    )  # fmt: skip
    handler = Capture()
    logging.getLogger("gateway").addHandler(handler)
    try:
        response = chat(TestClient(app))
    finally:
        logging.getLogger("gateway").removeHandler(handler)
    [line] = [json.loads(logs.JsonFormatter().format(r)) for r in handler.records if r.getMessage() == "request"]
    assert (response.status_code, line["status"], line["outcome"]) == (503, 503, "failed")
    assert line["error"] == "all_candidates_failed"
    assert [a["outcome"] for a in line["attempts"]] == ["unavailable", "unavailable"]  # the try and its one retry


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_guardrail_findings_are_logged_as_kinds_only(lines):
    import config

    route = config.Route(
        candidates=[config.Candidate(provider="stub", model="stub-echo")], max_tokens_ceiling=100,
        guardrails=config.Guardrails(pii_input="flag"),
    )  # fmt: skip
    app = gateway.create_app({"stub": route}, {"stub": StubProvider()}, {keys.key_hash(TEST_KEY): "test"})
    for stream in (False, True):  # a stream's findings arrive when it's settled, after the last event
        chat(TestClient(app), stream=stream)
    plain, streamed = lines()
    assert plain["guardrails"] == streamed["guardrails"] == "pii_input=flagged:card"
    assert "4111" not in json.dumps([plain, streamed])


def test_health_checks_are_not_logged(client, lines):
    client.get("/healthz")
    client.get("/readyz")
    assert lines() == []


def test_other_lines_carry_the_request_id_and_their_exception():
    record = logging.LogRecord("gateway.accounting", logging.ERROR, __file__, 1, "settling failed", None, None)
    try:
        raise RuntimeError("database went away")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    token = logs.REQUEST_ID.set("req-42")
    try:
        line = json.loads(logs.JsonFormatter().format(record))
    finally:
        logs.REQUEST_ID.reset(token)
    assert (line["request_id"], line["level"], line["logger"]) == ("req-42", "error", "gateway.accounting")
    assert "database went away" in line["exception"]
    assert "request_id" not in json.loads(logs.JsonFormatter().format(record))  # outside a request: no id


def test_decimal_costs_are_written_as_numbers():
    record = logging.LogRecord("gateway", logging.INFO, __file__, 1, "request", None, None)
    record.fields = {"cost_usd": Decimal("0.0070")}
    assert json.loads(logs.JsonFormatter().format(record))["cost_usd"] == 0.007


def test_configuring_twice_adds_one_handler():
    logs.configure()
    logs.configure()
    ours = [h for h in logging.getLogger("gateway").handlers if isinstance(h, logs._StdoutHandler)]
    assert len(ours) == 1
