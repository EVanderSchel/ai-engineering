"""Step 10 stage 4: the LangChain app against the gateway in memory, with the fake upstream behind it, and the fake
upstream's answers in the shape a request asks for. No network, no Docker. LiteLLM isn't here: it only runs in
Docker, so its side is measured by `src/langchain_app.py` (results/compare/langchain.md)."""

import asyncio
import json
import sys

import httpx2
import pytest
from conftest import TEST_KEY, no_wait_policy
from fastapi.testclient import TestClient

import app as gateway
import config
import fake_upstream
import keys
import langchain_app
from openai_compatible_provider import OpenAICompatibleProvider
from paths import ROOT


def run(coro):
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


def in_memory_model(**kwargs):
    """ChatOpenAI as the app builds it, but its HTTP client talks to the gateway app in this process."""
    upstream = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fake_upstream.app), base_url="http://fake/v1")
    app = gateway.create_app(
        config.load_routes(ROOT / "compare" / "routes.yaml"), {"local": OpenAICompatibleProvider(upstream)},
        {keys.key_hash(TEST_KEY): "compare"}, policy=no_wait_policy(),
    )  # fmt: skip
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
    key = kwargs.pop("api_key", TEST_KEY)
    return langchain_app.chat_model("http://gateway", key, http_async_client=client, **kwargs)


FEATURES = {name: feature for name, _, feature in langchain_app.FEATURES}


def outcome(name):
    return run(langchain_app.observe(FEATURES[name], in_memory_model))


@pytest.mark.parametrize(
    ("name", "observed"),
    [
        ("chain", "20 words"),
        ("batch", "5 of 5 answered"),
        ("token_counts", "in 13, out 20"),  # the fake upstream counts words
        ("sampling", "temperature 0.2 accepted, 20 words"),
        ("fallback", "20 words"),  # the overloaded first model is skipped inside the gateway
    ],
)
def test_the_app_works_through_the_gateway_unchanged(name, observed):
    assert outcome(name) == {"ok": True, "observed": observed}


def test_a_stream_arrives_in_pieces():
    result = outcome("streaming")
    assert result["ok"] and int(result["observed"].split()[0]) > 1


def test_a_streams_token_counts_need_asking_for():
    # ChatOpenAI leaves stream_options out for any base URL but OpenAI's, so a stream has no counts by default
    assert outcome("streamed_token_counts") == {"ok": True, "observed": "default: none; stream_usage=True: out 20"}


def test_the_gateways_headers_reach_the_app():
    result = outcome("response_headers")
    assert result["ok"] and result["observed"].startswith("4 gateway headers")


@pytest.mark.parametrize(("name", "field"), [("structured_output", "response_format"), ("tool_calling", "tools")])
def test_fields_the_gateway_doesnt_support_are_refused_by_name(name, field):
    result = outcome(name)
    assert not result["ok"] and result["observed"].startswith("OpenAIInvalidRequestError 400")
    assert field in result["observed"]


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("wrong_key", "OpenAIAuthenticationError 401"),
        ("unknown_model", "OpenAIModelNotFoundError 404"),
        ("no_max_tokens", "OpenAIInvalidRequestError 400"),  # the gateway requires a bound on the answer's cost
    ],
)
def test_errors_reach_the_app_as_the_openai_sdks_exceptions(name, error):
    result = outcome(name)
    assert not result["ok"] and result["observed"].startswith(error)


def test_the_report_marks_errors():
    result = {"date": "2026-11-13T00:00:00+00:00", "versions": "1.7.4, langchain-core 1.7.6",
              "features": [{"name": "chain", "what": "A chain", "gateway": {"ok": True, "observed": "20 words"},
                            "litellm": {"ok": False, "observed": "OpenAIInvalidRequestError 400: a | b"}}]}  # fmt: skip
    assert "| A chain | 20 words | **error** OpenAIInvalidRequestError 400: a \\| b |" in langchain_app.report(result)


# --- the fake upstream answers in the shape asked for ---


@pytest.fixture
def upstream():
    return TestClient(fake_upstream.app)


def test_a_request_with_tools_gets_a_call_to_the_first_tool(upstream):
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {
        "type": "object", "properties": {"name": {"type": "string"}, "limit": {"type": "integer"}}}}}  # fmt: skip
    body = {"model": "fake-ok", "messages": [{"role": "user", "content": "hi"}], "tools": [tool]}
    choice = upstream.post("/v1/chat/completions", json=body).json()["choices"][0]
    call = choice["message"]["tool_calls"][0]
    assert choice["finish_reason"] == "tool_calls" and call["function"]["name"] == "lookup"
    assert json.loads(call["function"]["arguments"]) == {"name": "x", "limit": 0}


def test_a_json_schema_request_gets_json_matching_it(upstream):
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}},
                                               "kind": {"enum": ["a", "b"]}, "ok": {"type": "boolean"}}}  # fmt: skip
    body = {"model": "fake-ok", "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "s", "schema": schema}}}  # fmt: skip
    content = upstream.post("/v1/chat/completions", json=body).json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {"tags": ["x"], "kind": "a", "ok": False}
