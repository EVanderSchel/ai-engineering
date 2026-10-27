"""The Claude API (api.anthropic.com) behind the provider interface.

Uses the official SDK on API version 2026-10-21, where Messages requests no longer accept temperature or top_p, so
routes with a Claude candidate refuse them (design decision 16). The SDK's own retries are off: the gateway decides
retries and fallbacks itself (decision 9, step 3), and two layers of retries would multiply waits and costs.
"""

from collections.abc import AsyncIterator

import anthropic

from providers import (
    Completion,
    Finish,
    ProviderError,
    ProviderRequest,
    StreamEvent,
    TextDelta,
    Usage,
)
from translate import finish_reason

NAME = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client(timeout: float = 120.0) -> anthropic.AsyncAnthropic:
    """Credentials come from the environment (ANTHROPIC_API_KEY), never from the gateway's config files."""
    return anthropic.AsyncAnthropic(max_retries=0, timeout=anthropic.Timeout(timeout, connect=5.0))


class AnthropicProvider:
    name = NAME
    supports_sampling = False  # API version 2026-10-21 rejects temperature, top_p and top_k

    def __init__(self, client: anthropic.AsyncAnthropic):
        self.client = client

    def _call(self, request: ProviderRequest) -> tuple[object, dict]:
        """The SDK resource to call and its arguments. Server-side refusal fallback needs the beta namespace."""
        kwargs: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": t.role, "content": t.text} for t in request.turns],
        }
        if request.system:
            block = {"type": "text", "text": request.system}
            if request.cache_system_prompt:
                block["cache_control"] = {"type": "ephemeral"}
            kwargs["system"] = [block]
        if request.stop:
            kwargs["stop_sequences"] = request.stop
        if request.effort:
            kwargs["output_config"] = {"effort": request.effort}
        if request.refusal_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
            return self.client.beta.messages, kwargs
        return self.client.messages, kwargs

    async def complete(self, request: ProviderRequest) -> Completion:
        api, kwargs = self._call(request)
        try:
            message = await api.create(**kwargs)
        except anthropic.APIError as error:
            raise to_provider_error(error) from error
        return to_completion(message)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        api, kwargs = self._call(request)
        try:
            async with api.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield TextDelta(text)
                message = await stream.get_final_message()
        except anthropic.APIError as error:
            raise to_provider_error(error) from error
        completion = to_completion(message, keep_partial_refusal=True)
        yield Finish(completion.finish_reason, completion.model, completion.usage)


def to_completion(message, keep_partial_refusal: bool = False) -> Completion:
    reason = finish_reason(message.stop_reason)
    # Thinking blocks (empty by default on current models) and fallback markers aren't part of the answer.
    text = "".join(block.text for block in message.content if block.type == "text")
    if reason == "content_filter" and not keep_partial_refusal:
        # A refusal can arrive after some output; that partial text isn't an answer, so it isn't returned.
        # (When streaming it has already been sent, and the finish reason tells the client it was cut off.)
        text = ""
    return Completion(text=text, finish_reason=reason, model=message.model, usage=to_usage(message.usage))


def to_usage(usage) -> Usage:
    return Usage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )


def to_provider_error(error: anthropic.APIError) -> ProviderError:
    """The SDK's error classes, most specific first, as the gateway's error kinds."""
    message = getattr(error, "message", str(error))
    if isinstance(error, anthropic.APITimeoutError):  # a subclass of APIConnectionError: check it first
        return ProviderError("timeout", "Anthropic API timed out", NAME)
    if isinstance(error, anthropic.APIConnectionError):
        return ProviderError("unavailable", "could not connect to the Anthropic API", NAME)
    if isinstance(error, anthropic.RateLimitError):
        retry_after = error.response.headers.get("retry-after")
        return ProviderError(
            "unavailable", "Anthropic API rate limit", NAME, float(retry_after) if retry_after else None
        )
    if isinstance(error, anthropic.RequestTooLargeError):
        return ProviderError("too_large", message, NAME)
    if isinstance(error, anthropic.BadRequestError | anthropic.UnprocessableEntityError):
        return ProviderError("rejected", message, NAME)
    if isinstance(error, anthropic.AuthenticationError | anthropic.PermissionDeniedError | anthropic.NotFoundError):
        # The gateway's own key or model name is wrong: the caller can't fix it, so the details stay in the logs.
        return ProviderError("misconfigured", "the gateway's Anthropic configuration was rejected", NAME)
    if isinstance(error, anthropic.APIStatusError) and error.status_code >= 500:
        return ProviderError("unavailable", f"Anthropic API error {error.status_code}", NAME)
    return ProviderError("bad_response", message, NAME)
