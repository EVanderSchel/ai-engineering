"""Claude on Amazon Bedrock behind the provider interface, through the Converse API.

Bedrock is a second, independent way to reach Claude: a different company's infrastructure, quotas and outages, so
a route can fall back from the Claude API to Bedrock (design decision 8). On this AWS project only Claude 4.x models
are available on Bedrock (Claude 5.x quotas are 0), so a fallback is often a different model, and the response says
which one answered.

Converse rather than the Anthropic SDK's Bedrock client: the SDK 2.x sends API version 2026-10-21, which Bedrock
accepts only for Claude 4.7 and later, and these routes use Sonnet 4.6 and Haiku 4.5. boto3 is synchronous, so
calls run in a worker thread; a stream's events are passed back to the event loop as they arrive.

Region and credentials come from the environment: AWS_REGION (default us-east-2, this project's Region) and boto3's
usual chain (AWS_PROFILE locally after `aws login`, an IAM role or injected credentials when deployed). boto3's own
retries are off: the gateway's router decides retries and fallbacks.
"""

import asyncio
import os
import threading
from collections.abc import AsyncIterator

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)

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

NAME = "bedrock"
DEFAULT_REGION = "us-east-2"

FINISH_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}

# Bedrock's error codes, as the gateway's error kinds.
ERROR_KINDS = {
    "ThrottlingException": "unavailable",
    "ServiceUnavailableException": "unavailable",
    "InternalServerException": "unavailable",
    "ModelNotReadyException": "unavailable",
    "ModelStreamErrorException": "unavailable",
    "ModelTimeoutException": "timeout",
    "ValidationException": "rejected",
    "AccessDeniedException": "misconfigured",
    "ResourceNotFoundException": "misconfigured",
    "UnrecognizedClientException": "misconfigured",
    "ExpiredTokenException": "misconfigured",
    "ModelErrorException": "bad_response",
}


def make_client(timeout: float = 120.0):
    """A bedrock-runtime client: no retries of its own, and the read timeout as a backstop to the router's."""
    session = boto3.Session(region_name=os.environ.get("AWS_REGION", DEFAULT_REGION))
    config = Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=timeout)
    return session.client("bedrock-runtime", config=config)


class BedrockProvider:
    name = NAME
    supports_sampling = True  # the Claude 4.x models on Bedrock still accept temperature and topP

    def __init__(self, client):
        self.client = client

    def _kwargs(self, request: ProviderRequest) -> dict:
        inference: dict = {"maxTokens": request.max_tokens}  # always set: Bedrock reserves quota against it
        if request.stop:
            inference["stopSequences"] = request.stop
        if request.temperature is not None:
            inference["temperature"] = request.temperature
        if request.top_p is not None:
            inference["topP"] = request.top_p
        kwargs = {
            "modelId": request.model,
            "messages": [{"role": t.role, "content": [{"text": t.text}]} for t in request.turns],
            "inferenceConfig": inference,
        }
        if request.system:
            kwargs["system"] = [{"text": request.system}]
            if request.cache_system_prompt:
                kwargs["system"].append({"cachePoint": {"type": "default"}})
        return kwargs

    async def complete(self, request: ProviderRequest) -> Completion:
        try:
            response = await asyncio.to_thread(self.client.converse, **self._kwargs(request))
        except (ClientError, BotoCoreError) as error:
            raise to_provider_error(error) from error
        content = response["output"]["message"]["content"]
        return Completion(
            text="".join(block.get("text", "") for block in content),
            finish_reason=FINISH_REASONS.get(response["stopReason"], "stop"),
            model=request.model,
            usage=to_usage(response["usage"]),
        )

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        try:
            response = await asyncio.to_thread(self.client.converse_stream, **self._kwargs(request))
        except (ClientError, BotoCoreError) as error:
            raise to_provider_error(error) from error
        events = response["stream"]
        finish: FinishReason = "stop"
        usage = Usage()
        try:
            async for event in _in_thread(events):
                if "contentBlockDelta" in event:
                    text = event["contentBlockDelta"]["delta"].get("text", "")
                    if text:
                        yield TextDelta(text)
                elif "messageStop" in event:
                    finish = FINISH_REASONS.get(event["messageStop"]["stopReason"], "stop")
                elif "metadata" in event:
                    usage = to_usage(event["metadata"]["usage"])
                else:
                    for key, kind in ERROR_KINDS.items():  # an error delivered as a stream event
                        lowered = key[0].lower() + key[1:]
                        if lowered in event:
                            raise ProviderError(kind, event[lowered].get("message", key), NAME)
        except (ClientError, BotoCoreError) as error:
            raise to_provider_error(error) from error
        finally:
            events.close()
        yield Finish(finish, request.model, usage)


async def _in_thread(iterable) -> AsyncIterator:
    """Iterate a blocking iterable in a worker thread, handing each item to the event loop as it arrives."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()
    stop = threading.Event()

    def pump():
        try:
            for item in iterable:
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(queue.put_nowait, (item, None))
        except Exception as error:  # handed to the consumer, which re-raises it
            loop.call_soon_threadsafe(queue.put_nowait, (None, error))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, (done, None))

    worker = asyncio.create_task(asyncio.to_thread(pump))
    try:
        while True:
            item, error = await queue.get()
            if error is not None:
                raise error
            if item is done:
                return
            yield item
    finally:
        stop.set()
        if worker.done():
            await worker


def to_usage(usage: dict) -> Usage:
    return Usage(
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        cache_read_tokens=usage.get("cacheReadInputTokens", 0),
        cache_write_tokens=usage.get("cacheWriteInputTokens", 0),
    )


def to_provider_error(error: Exception) -> ProviderError:
    if isinstance(error, ClientError):  # includes EventStreamError: an error raised partway through a stream
        code = error.response.get("Error", {}).get("Code", "")
        kind = ERROR_KINDS.get(code, "bad_response")
        if kind == "misconfigured":
            # The gateway's AWS access is wrong: the caller can't fix it, so the details stay in the logs.
            return ProviderError(kind, f"the gateway's Bedrock access was refused ({code})", NAME)
        message = error.response.get("Error", {}).get("Message", code)
        return ProviderError(kind, f"{code}: {message}", NAME)
    if isinstance(error, ReadTimeoutError | ConnectTimeoutError):
        return ProviderError("timeout", "Bedrock timed out", NAME)
    if isinstance(error, EndpointConnectionError):
        return ProviderError("unavailable", "could not connect to Bedrock", NAME)
    name = type(error).__name__
    # Missing or expired credentials (NoCredentialsError, CredentialRetrievalError, an expired `aws login`: LoginError)
    if isinstance(error, NoCredentialsError) or "Credential" in name or name.startswith("Login"):
        return ProviderError("misconfigured", "the gateway has no valid AWS credentials", NAME)
    return ProviderError("unavailable", f"Bedrock client error: {type(error).__name__}", NAME)
