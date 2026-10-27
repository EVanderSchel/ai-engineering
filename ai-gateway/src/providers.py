"""What every provider adapter takes and returns, so the gateway never deals with a provider's own types.

Plain language: each provider speaks its own dialect (Anthropic's Messages API, Bedrock's Converse, llama-server's
OpenAI format). An adapter translates between that dialect and these few shapes, so the rest of the gateway is
written once.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

FinishReason = Literal["stop", "length", "content_filter"]


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    system: str | None
    turns: list[Turn]  # alternating, starting and ending with "user"
    max_tokens: int
    stop: list[str] = field(default_factory=list)
    temperature: float | None = None
    top_p: float | None = None
    cache_system_prompt: bool = False
    effort: str | None = None  # Claude's output_config.effort, set per route candidate
    refusal_fallback: bool = False  # Claude API's server-side fallback on a refusal, set per route candidate


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class Completion:
    text: str
    finish_reason: FinishReason
    model: str  # the model that actually answered
    usage: Usage


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Finish:
    finish_reason: FinishReason
    model: str
    usage: Usage


StreamEvent = TextDelta | Finish

# What went wrong, in terms the gateway decides on (whether to retry, which status to return), not in a
# provider's own error classes.
ErrorKind = Literal[
    "timeout",  # no answer in time
    "unavailable",  # connection failed, rate limited by the provider, overloaded or a 5xx
    "rejected",  # the provider says the request itself is invalid
    "too_large",  # the request is over the provider's size limit
    "misconfigured",  # the gateway's credentials, model name or endpoint are wrong
    "bad_response",  # the provider answered with something unusable
]


class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, message: str, provider: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.provider = provider
        self.retry_after = retry_after


class Provider(Protocol):
    name: str
    supports_sampling: bool  # accepts temperature and top_p

    async def complete(self, request: ProviderRequest) -> Completion: ...

    def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        """Text deltas, then exactly one Finish. Raises ProviderError, before or during the stream."""
        ...
