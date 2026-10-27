"""The gateway service: one OpenAI-compatible endpoint in front of the providers.

    uv run uvicorn app:create_app --factory --app-dir src --port 8000

Step 2's request path: check the key, validate the request, pick the route's first candidate, call it, and answer as
OpenAI would, streamed or not. Limits, budgets, guardrails and fallback are added by later steps along this path.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

import anthropic_provider
import config
import keys
import schemas
from providers import Finish, Provider, ProviderError, StreamEvent, TextDelta
from stub_provider import StubProvider
from translate import InvalidMessages, to_provider_request

log = logging.getLogger("gateway")

REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        type_: str,
        code: str | None = None,
        param: str | None = None,
        headers: dict | None = None,
    ):
        super().__init__(message)
        self.status, self.message, self.type, self.code, self.param = status, message, type_, code, param
        self.headers = headers or {}

    def response(self) -> JSONResponse:
        body = schemas.error_body(self.message, self.type, self.code, self.param)
        return JSONResponse(body, status_code=self.status, headers=self.headers)


# How each provider error kind reaches the caller. No fallback yet (step 3), so a failing provider is the answer.
PROVIDER_ERRORS = {
    "timeout": (504, "timeout_error", "provider_timeout"),
    "unavailable": (503, "service_unavailable_error", "provider_unavailable"),
    "rejected": (400, "invalid_request_error", "provider_rejected_request"),
    "too_large": (413, "invalid_request_error", "request_too_large"),
    "misconfigured": (502, "api_error", "gateway_misconfigured"),
    "bad_response": (502, "api_error", "bad_provider_response"),
}


def from_provider_error(error: ProviderError) -> GatewayError:
    status, type_, code = PROVIDER_ERRORS[error.kind]
    headers = {"retry-after": str(int(error.retry_after))} if error.retry_after else None
    return GatewayError(status, f"{error.provider}: {error.message}", type_, code, headers=headers)


def create_app(
    routes: dict[str, config.Route] | None = None,
    providers: dict[str, Provider] | None = None,
    api_keys: dict[str, str] | None = None,
) -> FastAPI:
    """Everything the app depends on can be passed in, so tests run with the stub provider and test keys."""
    routes = routes if routes is not None else config.load_routes()
    if providers is None:
        providers = {"stub": StubProvider(), "anthropic": anthropic_provider.AnthropicProvider(make_anthropic())}
    api_keys = api_keys if api_keys is not None else keys.parse_keys(os.environ.get("GATEWAY_KEYS", ""))
    for name, route in routes.items():
        for candidate in route.candidates:
            if candidate.provider not in providers:
                raise ValueError(f"route {name!r} uses provider {candidate.provider!r}, which isn't configured")

    app = FastAPI(title="ai-gateway", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(_request: Request, error: GatewayError):
        return error.response()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, error: RequestValidationError):
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"] if part != "body")
        reason = "isn't supported" if first["type"] == "extra_forbidden" else first["msg"].lower()
        message = f"{field}: {reason}" if field else reason
        return GatewayError(400, message, "invalid_request_error", "invalid_request", param=field or None).response()

    def authenticate(request: Request) -> str:
        team = keys.team_for(request.headers.get("authorization"), api_keys)
        if team is None:
            raise GatewayError(401, "missing or invalid API key", "authentication_error", "invalid_api_key")
        return team

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready"}  # step 4 adds the database check

    @app.get("/v1/models")
    async def list_models(request: Request):
        authenticate(request)
        return {
            "object": "list",
            "data": [{"id": name, "object": "model", "created": 0, "owned_by": "ai-gateway"} for name in routes],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(body: schemas.ChatRequest, request: Request):
        team = authenticate(request)
        route = routes.get(body.model)
        if route is None:
            raise GatewayError(
                404, f"unknown model {body.model!r}; GET /v1/models lists the routes", "invalid_request_error",
                "model_not_found", param="model",
            )  # fmt: skip
        limit = body.output_limit()
        if limit is None:
            raise GatewayError(
                400, "max_tokens (or max_completion_tokens) is required: it bounds the cost of the request",
                "invalid_request_error", "max_tokens_required", param="max_tokens",
            )  # fmt: skip
        if limit > route.max_tokens_ceiling:
            raise GatewayError(
                400, f"max_tokens {limit} is over the {body.model!r} route's limit of {route.max_tokens_ceiling}",
                "invalid_request_error", "max_tokens_too_large", param="max_tokens",
            )  # fmt: skip
        for field in ("temperature", "top_p"):
            if getattr(body, field) is not None and not all(
                providers[c.provider].supports_sampling for c in route.candidates
            ):
                raise GatewayError(
                    400, f"{field} isn't supported on the {body.model!r} route: its Claude models use their "
                    "default sampling", "invalid_request_error", "unsupported_parameter", param=field,
                )  # fmt: skip

        candidate = route.candidates[0]
        provider = providers[candidate.provider]
        try:
            provider_request = to_provider_request(
                body, candidate.model, limit, effort=candidate.effort, refusal_fallback=candidate.refusal_fallback
            )
        except InvalidMessages as error:
            raise GatewayError(400, str(error), "invalid_request_error", "invalid_messages", "messages") from None

        log.info(
            "request %s team=%s route=%s provider=%s model=%s stream=%s",
            request.state.request_id, team, body.model, provider.name, candidate.model, body.stream,
        )  # fmt: skip
        headers = {"x-gateway-route": body.model, "x-gateway-provider": provider.name}
        deadline = asyncio.get_running_loop().time() + route.timeout.total

        if not body.stream:
            try:
                async with asyncio.timeout_at(deadline):
                    completion = await provider.complete(provider_request)
            except TimeoutError:
                raise GatewayError(504, "the provider didn't answer in time", "timeout_error", "timeout") from None
            except ProviderError as error:
                raise from_provider_error(error) from None
            return JSONResponse(schemas.completion_body(schemas.completion_id(), completion), headers=headers)

        # Streaming: wait for the first event before answering, so an error that happens before any output can
        # still be a proper HTTP status (and, in step 3, a fallback to the next candidate). Once the first byte is
        # sent the status is fixed at 200, and a later failure ends the stream with an error event (decision 10).
        events = provider.stream(provider_request)
        try:
            async with asyncio.timeout_at(deadline):
                first = await anext(events)
        except TimeoutError:
            await events.aclose()
            raise GatewayError(504, "the provider didn't start answering in time", "timeout_error", "timeout") from None
        except ProviderError as error:
            raise from_provider_error(error) from None
        include_usage = bool(body.stream_options and body.stream_options.include_usage)
        return StreamingResponse(
            sse(first, events, deadline, candidate.model, include_usage),
            media_type="text/event-stream",
            headers={**headers, "cache-control": "no-cache"},
        )

    return app


async def sse(
    first: StreamEvent, events: AsyncIterator[StreamEvent], deadline: float, model: str, include_usage: bool
) -> AsyncIterator[str]:
    """Server-sent events in OpenAI's chunk format, ending with `data: [DONE]`."""
    cid, created = schemas.completion_id(), int(time.time())

    def data(body: dict) -> str:
        return f"data: {json.dumps(body, separators=(',', ':'))}\n\n"

    yield data(schemas.chunk_body(cid, created, model, {"role": "assistant", "content": ""}))
    event = first
    try:
        while True:
            if isinstance(event, TextDelta):
                yield data(schemas.chunk_body(cid, created, model, {"content": event.text}))
            elif isinstance(event, Finish):
                yield data(schemas.chunk_body(cid, created, event.model, {}, event.finish_reason))
                if include_usage:
                    yield data(schemas.usage_chunk_body(cid, created, event.model, event.usage))
                yield "data: [DONE]\n\n"
                return
            async with asyncio.timeout_at(deadline):
                event = await anext(events)
    except StopAsyncIteration:
        error = GatewayError(502, "the provider's stream ended without finishing", "api_error", "incomplete_stream")
    except TimeoutError:
        error = GatewayError(504, "the provider didn't finish in time", "timeout_error", "timeout")
    except ProviderError as provider_error:
        error = from_provider_error(provider_error)
    finally:
        await events.aclose()
    yield data(schemas.error_body(error.message, error.type, error.code))


def make_anthropic():
    return anthropic_provider.make_client()
