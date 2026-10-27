"""The OpenAI-compatible request the gateway accepts (design decision 4), and the responses it sends.

Requests are validated strictly: a field the gateway doesn't support is rejected with a 400 naming it, never
silently dropped (decision 6). Responses are built as plain dicts in the shape OpenAI clients parse.
"""

import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextPart(Strict):
    type: Literal["text"]
    text: str


class Message(Strict):
    # "developer" is OpenAI's newer name for a system message; both are accepted.
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[TextPart]

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(part.text for part in self.content)


class StreamOptions(Strict):
    include_usage: bool = False


class GatewayOptions(Strict):
    """The gateway's own options, sent as an extra `gateway` object (`extra_body` in the OpenAI SDK)."""

    # Off unless asked: a cache write costs more than plain input, and only pays off when the prompt repeats.
    cache_system_prompt: bool = False


class ChatRequest(Strict):
    model: str  # a route name, not a provider model (decision 5)
    messages: list[Message] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)  # OpenAI's newer name for max_tokens
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    stop: str | list[str] | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    n: Literal[1] | None = None  # some clients always send n=1; more than one choice isn't supported
    user: str | None = None
    gateway: GatewayOptions = Field(default_factory=GatewayOptions)

    def output_limit(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens

    def stop_sequences(self) -> list[str]:
        if self.stop is None:
            return []
        return [self.stop] if isinstance(self.stop, str) else list(self.stop)


def completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def usage_dict(usage) -> dict:
    """OpenAI counts every input token as a prompt token, cached ones included, and reports the cached share."""
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    return {
        "prompt_tokens": prompt,
        "completion_tokens": usage.output_tokens,
        "total_tokens": prompt + usage.output_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cache_read_tokens},
    }


def completion_body(cid: str, completion) -> dict:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": completion.model,  # the provider model that answered, never hidden (decision 8)
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion.text},
                "finish_reason": completion.finish_reason,
                "logprobs": None,
            }
        ],
        "usage": usage_dict(completion.usage),
    }


def chunk_body(cid: str, created: int, model: str, delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}],
    }


def usage_chunk_body(cid: str, created: int, model: str, usage) -> dict:
    """The extra last chunk OpenAI sends when stream_options.include_usage is set: no choices, just usage."""
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": usage_dict(usage),
    }


def error_body(message: str, type_: str, code: str | None = None, param: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_, "code": code, "param": param}}
