"""Traces (tracing.py): one span per request with a child for each step, the same attributes as its log line, a
span per provider attempt, the caller's trace continued, and never any content. Spans go to memory here."""

import json
import logging

import pytest
from conftest import AUTH, TEST_KEY, no_wait_policy, routes
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app as gateway
import config
import keys
import logs
import router
from providers import ProviderError
from stub_provider import StubProvider

EXPORTER = InMemorySpanExporter()


@pytest.fixture
def spans():
    """The spans finished during the test. OpenTelemetry allows one provider per process, so it's set once."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
        trace.set_tracer_provider(provider)
    EXPORTER.clear()
    return lambda: list(EXPORTER.get_finished_spans())


def chat(client, **overrides):
    body = {"model": "stub", "max_tokens": 50, "messages": [{"role": "user", "content": "my ssn is 123-45-6789"}]}
    return client.post("/v1/chat/completions", json=body | overrides, headers=AUTH)


def by_name(spans) -> dict:
    return {s.name: s for s in spans}


def root_of(spans):
    """The request's span: FastAPI's server span, the one with no parent in this process."""
    [root] = [s for s in spans if s.kind == trace.SpanKind.SERVER]
    return root


def test_a_request_is_one_trace_with_a_span_per_step(client, spans):
    response = chat(client)
    found = by_name(spans())
    root = root_of(found.values())
    steps = {"gateway.auth", "gateway.admit", "gateway.guardrails.input", "chat stub-echo", "gateway.guardrails.output"}
    assert set(found) == steps | {root.name, "fastapi.endpoint", "fastapi.dependencies"}  # FastAPI's own spans too
    assert root.name == "POST /v1/chat/completions" and root.parent is None
    assert {s.context.trace_id for s in found.values()} == {root.context.trace_id}  # all one trace
    endpoint = found["fastapi.endpoint"]
    assert endpoint.parent.span_id == root.context.span_id
    assert all(found[name].parent.span_id == endpoint.context.span_id for name in steps)  # the handler's steps
    attributes = root.attributes
    assert attributes["gateway.request_id"] == response.headers["x-request-id"]
    assert (attributes["http.response.status_code"], attributes["gateway.team"], attributes["gateway.route"]) == (
        200, "test", "stub",
    )  # fmt: skip
    assert (attributes["gateway.outcome"], attributes["gen_ai.response.model"]) == ("ok", "stub-echo")
    assert attributes["gen_ai.usage.output_tokens"] == response.json()["usage"]["completion_tokens"]


def test_no_span_carries_the_messages_or_the_answer(client, spans):
    chat(client)
    dumped = json.dumps([dict(s.attributes) for s in spans()] + [e.name for s in spans() for e in s.events])
    assert "123-45-6789" not in dumped and "Stub reply" not in dumped


@pytest.fixture
def fallback_app():
    route = config.Route(
        candidates=[
            config.Candidate(provider="down", model="m1"),
            config.Candidate(provider="stub", model="stub-echo"),
        ],
        max_tokens_ceiling=100,
    )
    down = StubProvider("down", fail=ProviderError("unavailable", "down", "no"))
    return gateway.create_app(
        {"stub": route}, {"down": down, "stub": StubProvider()}, {keys.key_hash(TEST_KEY): "test"},
        policy=no_wait_policy(),
    )  # fmt: skip


def test_failed_attempts_and_the_fallback_are_separate_spans(fallback_app, spans):
    chat(TestClient(fallback_app))
    calls = sorted((s for s in spans() if s.name.startswith("chat ")), key=lambda s: s.start_time)
    summary = [(s.name, s.attributes["gateway.retry"], s.status.status_code, s.attributes.get("error.type"))
               for s in calls]  # fmt: skip
    assert summary == [
        ("chat m1", 0, trace.StatusCode.ERROR, "unavailable"),
        ("chat m1", 1, trace.StatusCode.ERROR, "unavailable"),  # the one retry
        ("chat stub-echo", 0, trace.StatusCode.UNSET, None),
    ]
    assert calls[0].attributes["gen_ai.provider.name"] == "down" and calls[0].kind == trace.SpanKind.CLIENT
    assert root_of(spans()).attributes["gateway.fallback"] is True


def test_a_skipped_candidate_is_an_event_not_a_call(spans):
    breakers = router.Breakers(lambda: router.Breaker(threshold=1))
    candidate = config.Candidate(provider="anthropic", model="claude-test")
    breakers[candidate].failure()  # open
    route = config.Route(candidates=[candidate, config.Candidate(provider="stub", model="stub-echo")],
                         max_tokens_ceiling=100)  # fmt: skip
    app = gateway.create_app(
        {"stub": route}, {"anthropic": StubProvider("anthropic"), "stub": StubProvider()},
        {keys.key_hash(TEST_KEY): "test"}, breakers=breakers,
    )  # fmt: skip
    chat(TestClient(app))
    found = by_name(spans())
    assert "chat claude-test" not in found
    [event] = found["fastapi.endpoint"].events  # on the handler's span, where the router runs
    assert event.name == "candidate skipped: breaker open"
    assert event.attributes["gen_ai.request.model"] == "claude-test"


def test_server_errors_mark_the_span_and_refusals_do_not(client, spans):
    chat(client, model="no-such-route")
    refused = root_of(spans())
    assert refused.status.status_code == trace.StatusCode.UNSET  # a 404 is the caller's mistake, not a failure
    assert refused.attributes["gateway.error"] == "model_not_found"

    EXPORTER.clear()
    down = StubProvider(fail=ProviderError("unavailable", "stub", "down"))
    app = gateway.create_app(routes(), {"stub": down, "anthropic": StubProvider("anthropic")},
                             {keys.key_hash(TEST_KEY): "test"}, policy=no_wait_policy())  # fmt: skip
    chat(TestClient(app))
    failed = root_of(spans())
    assert failed.status.status_code == trace.StatusCode.ERROR  # FastAPI's rule: 5xx
    assert (failed.attributes["error.type"], failed.attributes["gateway.error"]) == ("503", "all_candidates_failed")


def test_health_checks_are_not_traced(client, spans):
    client.get("/healthz")
    client.get("/readyz")
    assert spans() == []


def test_a_stream_span_ends_after_the_last_event_and_marks_the_first_token(client, spans):
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "stub", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi"}],
    }) as response:  # fmt: skip
        assert not [s for s in spans() if s.kind == trace.SpanKind.SERVER]  # still open while the body streams
        response.read()
    root = root_of(spans())
    assert [e.name for e in root.events] == ["first token"]
    assert root.attributes["gateway.stream"] is True


def test_the_callers_trace_is_continued(client, spans):
    parent_trace, parent_span = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    client.post(
        "/v1/chat/completions", headers=AUTH | {"traceparent": f"00-{parent_trace}-{parent_span}-01"},
        json={"model": "stub", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]},
    )  # fmt: skip
    root = root_of(spans())
    assert format(root.context.trace_id, "032x") == parent_trace
    assert format(root.parent.span_id, "016x") == parent_span


def test_the_log_line_names_the_trace(client, spans, caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    chat(client)
    [record] = [r for r in caplog.records if r.getMessage() == "request"]
    line = json.loads(logs.JsonFormatter().format(record))
    assert line["trace_id"] == format(root_of(spans()).context.trace_id, "032x")
