"""The adapter for OpenAI-compatible servers (llama-server), against a mock HTTP transport: what it sends, how it
reads answers and streams, and how it maps the server's errors. No server and no network."""

import json

import httpx2
import pytest

from openai_compatible_provider import OpenAICompatibleProvider, status_error
from providers import Finish, ProviderError, ProviderRequest, TextDelta, Turn


def request(**fields) -> ProviderRequest:
    base = {"model": "qwen2.5-0.5b-instruct", "system": None, "turns": [Turn("user", "hi")], "max_tokens": 50}
    return ProviderRequest(**{**base, **fields})


def provider(handler) -> tuple[OpenAICompatibleProvider, list]:
    seen = []

    def record(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return handler(req)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(record), base_url="http://llama:8080/v1")
    return OpenAICompatibleProvider(client), seen


def completion(text="Hello", finish="stop", usage=None):
    return {
        "id": "x", "object": "chat.completion", "model": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
    }  # fmt: skip


def sse(*chunks, done=True) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def chunk(content=None, finish=None, usage=None):
    body = {"model": "qwen-gguf", "choices": [{"index": 0, "delta": {"content": content} if content else {},
                                               "finish_reason": finish}]}  # fmt: skip
    if usage:
        body = {"model": "qwen-gguf", "choices": [], "usage": usage}
    return body


@pytest.mark.anyio
async def test_a_request_is_passed_through_in_openai_format():
    p, seen = provider(lambda r: httpx2.Response(200, json=completion()))
    result = await p.complete(request(system="Be brief.", stop=["END"], temperature=0.3, top_p=0.9))
    assert str(seen[0].url) == "http://llama:8080/v1/chat/completions"
    assert json.loads(seen[0].content) == {
        "model": "qwen2.5-0.5b-instruct",
        "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}],
        "max_tokens": 50, "stop": ["END"], "temperature": 0.3, "top_p": 0.9,
    }  # fmt: skip
    assert (result.text, result.finish_reason) == ("Hello", "stop")
    assert result.model == "qwen2.5-0.5b-instruct-q4_k_m.gguf"  # what the server says it ran
    assert (result.usage.input_tokens, result.usage.output_tokens) == (9, 2)


@pytest.mark.anyio
async def test_cached_prompt_tokens_are_counted_as_cache_reads():
    usage = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 80}}
    p, _ = provider(lambda r: httpx2.Response(200, json=completion(usage=usage)))
    result = await p.complete(request())
    assert (result.usage.input_tokens, result.usage.cache_read_tokens) == (20, 80)


@pytest.mark.anyio
async def test_a_length_finish_is_reported():
    p, _ = provider(lambda r: httpx2.Response(200, json=completion(finish="length")))
    assert (await p.complete(request())).finish_reason == "length"


@pytest.mark.anyio
async def test_an_answer_that_isnt_a_completion_is_a_bad_response():
    p, _ = provider(lambda r: httpx2.Response(200, json={"nope": True}))
    with pytest.raises(ProviderError) as raised:
        await p.complete(request())
    assert raised.value.kind == "bad_response"


@pytest.mark.anyio
async def test_a_stream_asks_for_usage_and_yields_text_then_one_finish():
    body = sse(
        chunk("Hel"), chunk("lo"), chunk(finish="length"), chunk(usage={"prompt_tokens": 9, "completion_tokens": 2})
    )
    p, seen = provider(lambda r: httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    events = [e async for e in p.stream(request())]
    sent = json.loads(seen[0].content)
    assert (sent["stream"], sent["stream_options"]) == (True, {"include_usage": True})
    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")]
    assert isinstance(events[2], Finish)
    assert (events[2].finish_reason, events[2].usage.output_tokens, events[2].model) == ("length", 2, "qwen-gguf")


@pytest.mark.anyio
async def test_an_error_event_in_a_stream_is_a_provider_error():
    body = sse(chunk("Hel"), {"error": {"message": "slot unavailable"}}, done=False)
    p, _ = provider(lambda r: httpx2.Response(200, content=body))
    seen = []
    with pytest.raises(ProviderError) as raised:
        async for event in p.stream(request()):
            seen.append(event)
    assert seen == [TextDelta("Hel")]
    assert raised.value.kind == "unavailable"


@pytest.mark.anyio
async def test_a_stream_refused_before_any_output_raises_its_status_kind():
    p, _ = provider(lambda r: httpx2.Response(503, json={"error": {"message": "Loading model"}}))
    with pytest.raises(ProviderError) as raised:
        [e async for e in p.stream(request())]
    assert raised.value.kind == "unavailable" and "Loading model" in raised.value.message


@pytest.mark.parametrize(
    "status, kind",
    [(400, "rejected"), (413, "too_large"), (401, "misconfigured"), (404, "misconfigured"),
     (429, "unavailable"), (500, "unavailable"), (503, "unavailable"), (418, "bad_response")],
)  # fmt: skip
def test_http_statuses_become_gateway_error_kinds(status, kind):
    assert status_error(status, '{"error": {"message": "x"}}', "local").kind == kind


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error, kind",
    [(httpx2.ConnectError("refused"), "unavailable"), (httpx2.ReadTimeout("slow"), "timeout")],
)
async def test_transport_failures_become_gateway_error_kinds(error, kind):
    def fail(req):
        raise error

    p, _ = provider(fail)
    with pytest.raises(ProviderError) as raised:
        await p.complete(request())
    assert raised.value.kind == kind
    with pytest.raises(ProviderError):
        [e async for e in p.stream(request())]
