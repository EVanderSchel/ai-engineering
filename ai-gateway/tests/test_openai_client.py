"""The real proof of decision 4: the official OpenAI Python client talks to the gateway with only its base URL
changed. The client's HTTP calls go straight into the app in memory (an ASGI transport), so nothing leaves the
process."""

import httpx2
import openai
import pytest
from conftest import TEST_KEY


@pytest.fixture
def openai_client(app):
    transport = httpx2.ASGITransport(app=app)
    return openai.AsyncOpenAI(
        api_key=TEST_KEY,
        base_url="http://gateway/v1",
        http_client=httpx2.AsyncClient(transport=transport, base_url="http://gateway"),
        max_retries=0,
    )


@pytest.mark.anyio
async def test_a_completion_through_the_openai_client(openai_client):
    response = await openai_client.chat.completions.create(
        model="stub", max_tokens=50, messages=[{"role": "user", "content": "ping"}]
    )
    assert response.choices[0].message.content == "Stub reply to: ping"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage.total_tokens == 5


@pytest.mark.anyio
async def test_a_stream_through_the_openai_client(openai_client):
    stream = await openai_client.chat.completions.create(
        model="stub", max_tokens=50, stream=True, stream_options={"include_usage": True},
        messages=[{"role": "user", "content": "ping"}],
    )  # fmt: skip
    text, usage = "", None
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
        if chunk.usage:
            usage = chunk.usage
    assert text == "Stub reply to: ping"
    assert usage.completion_tokens == 4


@pytest.mark.anyio
async def test_gateway_errors_arrive_as_the_clients_own_exceptions(openai_client):
    with pytest.raises(openai.NotFoundError):
        await openai_client.chat.completions.create(
            model="no-such-route", max_tokens=5, messages=[{"role": "user", "content": "x"}]
        )
    with pytest.raises(openai.BadRequestError, match="tools"):
        await openai_client.chat.completions.create(
            model="stub", max_tokens=5, messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
        )  # fmt: skip
    bad_key = openai_client.with_options(api_key="gw_wrong")
    with pytest.raises(openai.AuthenticationError):
        await bad_key.chat.completions.create(model="stub", max_tokens=5, messages=[{"role": "user", "content": "x"}])


@pytest.mark.anyio
async def test_gateway_options_go_in_extra_body(openai_client, claude_stub):
    await openai_client.chat.completions.create(
        model="claude", max_tokens=5, messages=[{"role": "user", "content": "x"}],
        extra_body={"gateway": {"cache_system_prompt": True}},
    )  # fmt: skip
    assert claude_stub.requests[-1].cache_system_prompt is True


@pytest.mark.anyio
async def test_models_list_through_the_openai_client(openai_client):
    assert [m.id async for m in openai_client.models.list()] == ["stub", "claude"]
