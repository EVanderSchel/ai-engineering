import json

import pytest
from conftest import AUTH, TEST_KEY, routes
from fastapi.testclient import TestClient

import app as gateway
import keys
from providers import ProviderError


def chat(client, **overrides):
    body = {"model": "stub", "max_tokens": 50, "messages": [{"role": "user", "content": "hello there"}]}
    body.update(overrides)
    return client.post("/v1/chat/completions", json=body, headers=AUTH)


def sse_events(response) -> list:
    """The `data:` payloads of a server-sent event stream, parsed (except the final [DONE] marker)."""
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            payload = line.removeprefix("data: ")
            events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


def test_health_endpoints_need_no_key(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_a_missing_or_wrong_key_is_401(client):
    body = {"model": "stub", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat/completions", json=body).status_code == 401
    wrong = client.post("/v1/chat/completions", json=body, headers={"authorization": "Bearer gw_not-a-real-key"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_api_key"


def test_models_lists_the_routes(client):
    ids = [m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]]
    assert ids == ["stub", "claude"]


def test_a_completion_has_openais_shape(client):
    response = chat(client)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "stub-echo"  # the provider model that answered, not the route name
    choice = body["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "Stub reply to: hello there"}
    assert choice["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 5,
        "total_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    assert response.headers["x-gateway-route"] == "stub"
    assert response.headers["x-gateway-provider"] == "stub"


def test_every_response_carries_a_request_id(client):
    assert len(chat(client).headers["x-request-id"]) == 32
    echoed = client.get("/healthz", headers={"x-request-id": "trace-123"})
    assert echoed.headers["x-request-id"] == "trace-123"
    unsafe = client.get("/healthz", headers={"x-request-id": "bad id\twith spaces"})
    assert unsafe.headers["x-request-id"] != "bad id\twith spaces"


def test_max_completion_tokens_is_accepted_and_limits_the_answer(client):
    body = chat(client, max_tokens=None, max_completion_tokens=2).json()
    assert body["choices"][0]["message"]["content"] == "Stub reply"
    assert body["choices"][0]["finish_reason"] == "length"


def test_max_tokens_is_required_and_capped_per_route(client):
    missing = chat(client, max_tokens=None)
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "max_tokens_required"
    over = chat(client, max_tokens=101)
    assert over.status_code == 400
    assert over.json()["error"]["code"] == "max_tokens_too_large"


def test_an_unknown_route_is_404(client):
    response = chat(client, model="claude-opus-5-5")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_unsupported_fields_are_rejected_by_name(client):
    """Decision 6: never silently dropped."""
    response = chat(client, tools=[{"type": "function", "function": {"name": "f"}}])
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "tools"
    assert "isn't supported" in error["message"]
    assert chat(client, n=2).status_code == 400
    assert chat(client, n=1).status_code == 200


def test_sampling_is_refused_on_routes_with_claude_and_allowed_elsewhere(client, stub):
    refused = chat(client, model="claude", temperature=0.2)
    assert refused.status_code == 400
    assert refused.json()["error"]["param"] == "temperature"
    assert chat(client, model="claude", top_p=0.9).status_code == 400
    assert chat(client, temperature=0.2, top_p=0.9).status_code == 200
    assert (stub.requests[-1].temperature, stub.requests[-1].top_p) == (0.2, 0.9)


def test_route_options_reach_the_provider(client, claude_stub):
    chat(client, model="claude", gateway={"cache_system_prompt": True},
         messages=[{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}])  # fmt: skip
    sent = claude_stub.requests[-1]
    assert (sent.model, sent.effort, sent.system, sent.cache_system_prompt) == ("claude-test", "low", "Be brief.", True)


def test_misplaced_messages_are_400(client):
    late_system = [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]
    assert chat(client, messages=late_system).json()["error"]["code"] == "invalid_messages"
    prefill = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Sure"}]
    assert chat(client, messages=prefill).status_code == 400


@pytest.mark.parametrize(
    "error, status, code",
    [
        (ProviderError("timeout", "slow", "stub"), 504, "timeout"),
        (ProviderError("unavailable", "rate limit", "stub", retry_after=7), 503, "all_candidates_failed"),
        (ProviderError("misconfigured", "bad key", "stub"), 503, "all_candidates_failed"),
        (ProviderError("rejected", "bad input", "stub"), 400, "provider_rejected_request"),
        (ProviderError("too_large", "too big", "stub"), 413, "request_too_large"),
    ],
)
def test_when_no_candidate_answers_the_status_says_why(client, stub, error, status, code):
    stub.fail = error
    response = chat(client)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    if error.kind in ("rejected", "too_large"):
        assert response.json()["error"]["message"] == f"stub: {error.message}"  # the provider's own reason
    else:
        assert "stub/stub-echo" in response.json()["error"]["message"]  # which candidates were tried
    assert response.headers.get("retry-after") == ("7" if error.retry_after else None)


def test_a_stream_is_openai_chunks_ending_with_done(client):
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "stub", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi"}],
    }) as response:  # fmt: skip
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = sse_events(response)
    assert events[-1] == "[DONE]"
    chunks = events[:-1]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert len({c["id"] for c in chunks}) == 1
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Stub reply to: hi"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert "usage" not in chunks[-1]


def test_include_usage_adds_a_last_chunk_with_usage(client):
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "stub", "max_tokens": 50, "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "hi"}],
    }) as response:  # fmt: skip
        events = sse_events(response)
    usage_chunk = events[-2]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"]["completion_tokens"] == 4


def test_a_stream_that_fails_before_output_gets_a_real_status(client, stub):
    stub.fail = ProviderError("unavailable", "down", "stub")
    response = chat(client, stream=True)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "all_candidates_failed"


def test_a_stream_that_fails_partway_ends_with_an_error_event(client, stub):
    """Decision 10: after the first byte the status is 200; the failure arrives as an error event, without [DONE]."""
    stub.fail, stub.fail_after_words = ProviderError("unavailable", "connection lost", "stub"), 2
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "stub", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi"}],
    }) as response:  # fmt: skip
        assert response.status_code == 200
        events = sse_events(response)
    assert "[DONE]" not in events
    assert events[-1]["error"]["code"] == "provider_unavailable"
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in events[:-1]) == "Stub reply"


def test_a_slow_provider_hits_the_route_deadline(stub, claude_stub):
    short = routes()
    short["stub"].timeout.total = 0.05
    stub.delay = 0.1
    app = gateway.create_app(short, {"stub": stub, "anthropic": claude_stub}, {keys.key_hash(TEST_KEY): "t"})
    client = TestClient(app)
    assert chat(client).status_code == 504
    assert chat(client, stream=True).status_code == 504


def test_a_route_naming_an_unconfigured_provider_stops_startup(stub):
    with pytest.raises(ValueError, match="isn't configured"):
        gateway.create_app(routes(), {"stub": stub}, {})
