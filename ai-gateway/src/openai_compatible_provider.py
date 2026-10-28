"""Any server that speaks OpenAI's chat completions API, behind the provider interface: here, llama-server (llama.cpp)
running a small self-hosted model (step 6), and in principle vLLM or another OpenAI-compatible server.

Plain language: llama-server already speaks the same language as the gateway's own API, so this adapter mostly
passes the request through and reads the answer back. What it adds is the same treatment every provider gets: the
same error kinds, timeouts, retries and fallback decided by the router.

The server's address comes from the environment (GATEWAY_LOCAL_URL, default http://localhost:8080/v1); in the
Kubernetes cluster it's the llama-server service's in-cluster address. No API key by default: the server is only
reachable inside the cluster.
"""

import json
import os
from collections.abc import AsyncIterator

import httpx2

from providers import (
    Completion,
    Finish,
    FinishReason,
    ProviderError,
    ProviderRequest,
    StreamEvent,
    TextDelta,
    Usage,
)

NAME = "local"
DEFAULT_URL = "http://localhost:8080/v1"

FINISH_REASONS: dict[str, FinishReason] = {"stop": "stop", "length": "length", "content_filter": "content_filter"}


def make_client(timeout: float = 120.0) -> httpx2.AsyncClient:
    base_url = os.environ.get("GATEWAY_LOCAL_URL", DEFAULT_URL).rstrip("/")
    headers = {}
    if api_key := os.environ.get("GATEWAY_LOCAL_API_KEY"):  # llama-server's --api-key, if it's set
        headers["authorization"] = f"Bearer {api_key}"
    return httpx2.AsyncClient(base_url=base_url, headers=headers, timeout=httpx2.Timeout(timeout, connect=5.0))


class OpenAICompatibleProvider:
    supports_sampling = True

    def __init__(self, client: httpx2.AsyncClient, name: str = NAME):
        self.client, self.name = client, name

    def _body(self, request: ProviderRequest, stream: bool) -> dict:
        messages = [{"role": "system", "content": request.system}] if request.system else []
        messages += [{"role": t.role, "content": t.text} for t in request.turns]
        body: dict = {"model": request.model, "messages": messages, "max_tokens": request.max_tokens}
        if request.stop:
            body["stop"] = request.stop
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    async def complete(self, request: ProviderRequest) -> Completion:
        try:
            response = await self.client.post("/chat/completions", json=self._body(request, stream=False))
        except httpx2.HTTPError as error:
            raise transport_error(error, self.name) from error
        if response.status_code != 200:
            raise status_error(response.status_code, response.text, self.name)
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except ValueError, KeyError, IndexError, TypeError:
            raise ProviderError("bad_response", "the server's answer wasn't a chat completion", self.name) from None
        return Completion(
            text=text,
            finish_reason=FINISH_REASONS.get(choice.get("finish_reason") or "stop", "stop"),
            model=data.get("model") or request.model,
            usage=to_usage(data.get("usage")),
        )

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        finish: FinishReason = "stop"
        usage, model = Usage(), request.model
        try:
            async with self.client.stream(
                "POST", "/chat/completions", json=self._body(request, stream=True)
            ) as response:
                if response.status_code != 200:
                    raise status_error(
                        response.status_code, (await response.aread()).decode(errors="replace"), self.name
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        raise ProviderError("bad_response", "the server sent an unreadable event", self.name) from None
                    if "error" in chunk:
                        raise ProviderError(
                            "unavailable", str(chunk["error"].get("message", "stream error")), self.name
                        )
                    model = chunk.get("model") or model
                    if chunk.get("usage"):
                        usage = to_usage(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield TextDelta(text)
                        if choice.get("finish_reason"):
                            finish = FINISH_REASONS.get(choice["finish_reason"], "stop")
        except httpx2.HTTPError as error:
            raise transport_error(error, self.name) from error
        yield Finish(finish, model, usage)


def to_usage(usage: dict | None) -> Usage:
    if not usage:
        return Usage()
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    return Usage(
        input_tokens=(usage.get("prompt_tokens") or 0) - cached,
        output_tokens=usage.get("completion_tokens") or 0,
        cache_read_tokens=cached,
    )


def status_error(status: int, body: str, provider: str) -> ProviderError:
    try:
        message = json.loads(body)["error"]["message"]
    except ValueError, KeyError, TypeError:
        message = body[:200] or f"HTTP {status}"
    if status in (401, 403, 404):
        return ProviderError("misconfigured", f"the server refused the gateway's request (HTTP {status})", provider)
    if status == 413:
        return ProviderError("too_large", message, provider)
    if status in (400, 422):
        return ProviderError("rejected", message, provider)
    if status == 429 or status >= 500:  # llama-server answers 503 while its model is still loading
        return ProviderError("unavailable", f"HTTP {status}: {message}", provider)
    return ProviderError("bad_response", f"HTTP {status}: {message}", provider)


def transport_error(error: httpx2.HTTPError, provider: str) -> ProviderError:
    if isinstance(error, httpx2.TimeoutException):
        return ProviderError("timeout", "the server timed out", provider)
    return ProviderError("unavailable", f"could not reach the server ({type(error).__name__})", provider)
