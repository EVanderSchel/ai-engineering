"""The Claude API adapter, against a fake SDK client: what it sends, how it reads answers, how it maps errors."""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from anthropic_provider import FALLBACK_BETA, AnthropicProvider, to_provider_error
from providers import Finish, ProviderError, ProviderRequest, TextDelta, Turn


def message(text="Hello", stop_reason="end_turn", model="claude-test", **usage):
    blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    counts = {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return SimpleNamespace(
        content=blocks, stop_reason=stop_reason, model=model, usage=SimpleNamespace(**{**counts, **usage})
    )


class FakeStream:
    def __init__(self, parts, final):
        self.parts, self.final = parts, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for part in self.parts:
                yield part

        return gen()

    async def get_final_message(self):
        return self.final


class FakeMessages:
    def __init__(self, result=None, error=None, parts=("Hel", "lo")):
        self.result, self.error, self.parts = result or message(), error, parts
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return FakeStream(self.parts, self.result)


def fake_client(**kwargs):
    plain, beta = FakeMessages(**kwargs), FakeMessages(**kwargs)
    return SimpleNamespace(messages=plain, beta=SimpleNamespace(messages=beta)), plain, beta


def request(**fields) -> ProviderRequest:
    base = {"model": "claude-test", "system": None, "turns": [Turn("user", "hi")], "max_tokens": 50}
    return ProviderRequest(**{**base, **fields})


@pytest.mark.anyio
async def test_a_plain_request_sends_only_what_claude_accepts():
    client, plain, beta = fake_client()
    completion = await AnthropicProvider(client).complete(request(stop=["END"]))
    assert plain.calls == [{
        "model": "claude-test", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}],
        "stop_sequences": ["END"],
    }]  # fmt: skip
    assert beta.calls == []
    assert (completion.text, completion.finish_reason, completion.model) == ("Hello", "stop", "claude-test")


@pytest.mark.anyio
async def test_system_caching_effort_and_refusal_fallback_are_sent():
    client, plain, beta = fake_client()
    await AnthropicProvider(client).complete(
        request(system="Be brief.", cache_system_prompt=True, effort="low", refusal_fallback=True)
    )
    sent = beta.calls[0]
    assert sent["system"] == [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}]
    assert sent["output_config"] == {"effort": "low"}
    assert (sent["betas"], sent["fallbacks"]) == ([FALLBACK_BETA], "default")
    assert "temperature" not in sent and "top_p" not in sent
    assert plain.calls == []


@pytest.mark.anyio
async def test_usage_counts_cached_input_separately():
    client, *_ = fake_client(result=message(cache_read_input_tokens=100, cache_creation_input_tokens=20))
    usage = (await AnthropicProvider(client).complete(request())).usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens, usage.output_tokens) == (
        10, 100, 20, 3,
    )  # fmt: skip


@pytest.mark.anyio
async def test_a_refusal_returns_no_partial_text_and_content_filter():
    client, *_ = fake_client(result=message(text="Partial", stop_reason="refusal"))
    completion = await AnthropicProvider(client).complete(request())
    assert (completion.text, completion.finish_reason) == ("", "content_filter")


@pytest.mark.anyio
async def test_the_answering_model_is_reported_after_a_server_side_fallback():
    client, *_ = fake_client(result=message(model="claude-sonnet-5"))
    assert (await AnthropicProvider(client).complete(request(refusal_fallback=True))).model == "claude-sonnet-5"


@pytest.mark.anyio
async def test_streaming_yields_text_then_one_finish():
    client, *_ = fake_client(result=message(text="Hello", stop_reason="max_tokens"), parts=("Hel", "", "lo"))
    events = [e async for e in AnthropicProvider(client).stream(request())]
    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")]
    assert isinstance(events[2], Finish) and events[2].finish_reason == "length"


def status_error(cls, status, headers=None):
    response = httpx2.Response(status, headers=headers, request=httpx2.Request("POST", "https://api.example"))
    return cls("upstream message", response=response, body=None)


@pytest.mark.parametrize(
    "error, kind",
    [
        (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.example")), "timeout"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.example")), "unavailable"),
        (status_error(anthropic.RateLimitError, 429), "unavailable"),
        (status_error(anthropic.InternalServerError, 500), "unavailable"),
        (status_error(anthropic.OverloadedError, 529), "unavailable"),
        (status_error(anthropic.BadRequestError, 400), "rejected"),
        (status_error(anthropic.RequestTooLargeError, 413), "too_large"),
        (status_error(anthropic.AuthenticationError, 401), "misconfigured"),
        (status_error(anthropic.NotFoundError, 404), "misconfigured"),
    ],
)
def test_sdk_errors_become_gateway_error_kinds(error, kind):
    assert to_provider_error(error).kind == kind


def test_a_rate_limit_keeps_retry_after_and_auth_errors_hide_details():
    assert to_provider_error(status_error(anthropic.RateLimitError, 429, {"retry-after": "12"})).retry_after == 12
    assert "upstream message" not in to_provider_error(status_error(anthropic.AuthenticationError, 401)).message


@pytest.mark.anyio
async def test_errors_are_raised_as_provider_errors_from_both_paths():
    client, *_ = fake_client(error=status_error(anthropic.RateLimitError, 429))
    provider = AnthropicProvider(client)
    with pytest.raises(ProviderError):
        await provider.complete(request())
    with pytest.raises(ProviderError):
        [e async for e in provider.stream(request())]
