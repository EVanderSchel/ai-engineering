"""The Bedrock adapter, against a fake bedrock-runtime client: what it sends, how it reads answers and streams, and
how it maps Bedrock's errors. No AWS calls."""

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    EventStreamError,
    NoCredentialsError,
    ReadTimeoutError,
)
from pydantic import ValidationError

import config
from bedrock_provider import BedrockProvider, to_provider_error
from providers import Finish, ProviderError, ProviderRequest, TextDelta, Turn

USAGE = {"inputTokens": 12, "outputTokens": 4, "cacheReadInputTokens": 100, "cacheWriteInputTokens": 0}


class FakeEventStream:
    def __init__(self, events, error=None):
        self.events, self.error, self.closed = events, error, False

    def __iter__(self):
        yield from self.events
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class FakeBedrock:
    def __init__(self, stop_reason="end_turn", text="Hello", events=None, error=None, stream_error=None):
        self.stop_reason, self.text, self.error = stop_reason, text, error
        self.stream = FakeEventStream(events if events is not None else default_events(), stream_error)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": self.text}]}},
            "stopReason": self.stop_reason,
            "usage": USAGE,
            "metrics": {"latencyMs": 300},
        }

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"stream": self.stream}


def default_events(stop="end_turn"):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Hel"}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": "lo"}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": stop}},
        {"metadata": {"usage": USAGE, "metrics": {"latencyMs": 300}}},
    ]


def request(**fields) -> ProviderRequest:
    base = {"model": "us.anthropic.claude-sonnet-4-6", "system": None, "turns": [Turn("user", "hi")], "max_tokens": 50}
    return ProviderRequest(**{**base, **fields})


def client_error(code, message="details", operation="Converse"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


@pytest.mark.anyio
async def test_a_request_becomes_a_converse_call():
    fake = FakeBedrock()
    completion = await BedrockProvider(fake).complete(request(stop=["END"], temperature=0.2))
    assert fake.calls == [{
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "inferenceConfig": {"maxTokens": 50, "stopSequences": ["END"], "temperature": 0.2},
    }]  # fmt: skip
    assert (completion.text, completion.finish_reason, completion.model) == (
        "Hello", "stop", "us.anthropic.claude-sonnet-4-6",
    )  # fmt: skip
    assert (completion.usage.input_tokens, completion.usage.cache_read_tokens) == (12, 100)


@pytest.mark.anyio
async def test_a_cached_system_prompt_gets_a_cache_point():
    fake = FakeBedrock()
    await BedrockProvider(fake).complete(request(system="Be brief.", cache_system_prompt=True))
    assert fake.calls[0]["system"] == [{"text": "Be brief."}, {"cachePoint": {"type": "default"}}]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stop, finish",
    [("max_tokens", "length"), ("content_filtered", "content_filter"), ("guardrail_intervened", "content_filter")],
)
async def test_stop_reasons_map_to_finish_reasons(stop, finish):
    assert (await BedrockProvider(FakeBedrock(stop_reason=stop)).complete(request())).finish_reason == finish


@pytest.mark.anyio
async def test_a_stream_yields_text_then_one_finish_with_usage():
    fake = FakeBedrock(events=default_events("max_tokens"))
    events = [e async for e in BedrockProvider(fake).stream(request())]
    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")]
    assert isinstance(events[2], Finish)
    assert (events[2].finish_reason, events[2].usage.output_tokens) == ("length", 4)
    assert fake.stream.closed


@pytest.mark.anyio
async def test_an_error_partway_through_a_stream_is_a_provider_error():
    error = EventStreamError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ConverseStream")
    fake = FakeBedrock(events=default_events()[:2], stream_error=error)
    seen = []
    with pytest.raises(ProviderError) as raised:
        async for event in BedrockProvider(fake).stream(request()):
            seen.append(event)
    assert seen == [TextDelta("Hel")]
    assert raised.value.kind == "unavailable"
    assert fake.stream.closed


@pytest.mark.anyio
async def test_an_error_event_in_the_stream_is_a_provider_error():
    fake = FakeBedrock(events=[{"internalServerException": {"message": "boom"}}])
    with pytest.raises(ProviderError) as raised:
        [e async for e in BedrockProvider(fake).stream(request())]
    assert raised.value.kind == "unavailable"


@pytest.mark.anyio
async def test_errors_before_any_output_are_raised_from_both_paths():
    for method in ("complete", "stream"):
        provider = BedrockProvider(FakeBedrock(error=client_error("ThrottlingException")))
        with pytest.raises(ProviderError):
            if method == "complete":
                await provider.complete(request())
            else:
                [e async for e in provider.stream(request())]


@pytest.mark.parametrize(
    "error, kind",
    [
        (client_error("ThrottlingException"), "unavailable"),
        (client_error("ServiceUnavailableException"), "unavailable"),
        (client_error("ModelTimeoutException"), "timeout"),
        (client_error("ValidationException"), "rejected"),
        (client_error("AccessDeniedException"), "misconfigured"),
        (client_error("ResourceNotFoundException"), "misconfigured"),
        (client_error("SomethingNew"), "bad_response"),
        (ReadTimeoutError(endpoint_url="https://bedrock"), "timeout"),
        (EndpointConnectionError(endpoint_url="https://bedrock"), "unavailable"),
        (NoCredentialsError(), "misconfigured"),
    ],
)
def test_bedrock_errors_become_gateway_error_kinds(error, kind):
    assert to_provider_error(error).kind == kind


def test_access_errors_keep_aws_details_out_of_the_message():
    error = to_provider_error(client_error("AccessDeniedException", "arn:aws:iam::123456789012:role/secret"))
    assert "123456789012" not in error.message


def test_claude_api_options_are_refused_on_other_providers():
    with pytest.raises(ValidationError, match="only apply to provider anthropic"):
        config.Candidate(provider="bedrock", model="m", effort="low")
    with pytest.raises(ValidationError):
        config.Candidate(provider="bedrock", model="m", refusal_fallback=True)
    assert config.Candidate(provider="anthropic", model="m", effort="low").effort == "low"
