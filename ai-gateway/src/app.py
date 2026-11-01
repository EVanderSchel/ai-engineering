"""The gateway service: one OpenAI-compatible endpoint in front of the providers.

    uv run uvicorn app:create_app --factory --app-dir src --port 8000

The request path: check the key, validate the request, try the route's candidates in order (router.py: retries,
fallback, circuit breakers), and answer as OpenAI would, streamed or not. Limits, budgets and guardrails are added
by later steps along this path.
"""

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

import accounting
import anthropic_provider
import bedrock_provider
import config
import db
import guardrails
import injection
import keys
import openai_compatible_provider
import router
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


# How a provider error kind reaches the caller, when it's the one that decides the answer (a refused request).
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
    api_keys: dict[str, str] | keys.KeyStore | None = None,
    breakers: router.Breakers | None = None,
    policy: router.Policy | None = None,
    judge: injection.Judge | None = None,
) -> FastAPI:
    """Everything the app depends on can be passed in, so tests run with the stub provider and test keys.

    `api_keys`: a key store, or {hash: team} for an in-memory one. Left out, keys come from PostgreSQL when
    DATABASE_URL is set (a connection pool opened when the app starts), else from GATEWAY_KEYS.
    `judge`: the injection judge, for routes or teams that ask for it. Left out, one on the Claude API client.
    """
    routes = routes if routes is not None else config.load_routes()
    judge = judge or injection.Judge()  # its Claude client is made on first use
    if providers is None:
        claude = anthropic_provider.make_client()
        providers = {
            "stub": StubProvider(),
            "anthropic": anthropic_provider.AnthropicProvider(claude),
            "bedrock": bedrock_provider.BedrockProvider(bedrock_provider.make_client()),
            # llama-server (step 6). Nothing connects until a route uses it.
            "local": openai_compatible_provider.OpenAICompatibleProvider(openai_compatible_provider.make_client()),
        }
    pool = None
    if isinstance(api_keys, dict):
        key_store: keys.KeyStore = keys.MemoryKeyStore(api_keys)
    elif api_keys is not None:
        key_store = api_keys
    elif url := os.environ.get("DATABASE_URL"):
        pool = db.make_pool(url)
        key_store = keys.PostgresKeyStore(pool)
    else:
        key_store = keys.MemoryKeyStore(keys.parse_keys(os.environ.get("GATEWAY_KEYS", "")))
    for name, route in routes.items():
        for candidate in route.candidates:
            if candidate.provider not in providers:
                raise ValueError(f"route {name!r} uses provider {candidate.provider!r}, which isn't configured")
    breakers = breakers or router.Breakers()
    policy = policy or router.Policy()
    ledger = accounting.Accounting(pool) if pool is not None else accounting.NoAccounting()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        if pool is not None:
            await pool.open(wait=True, timeout=10)
            await db.check_schema(pool)  # refuses to start against a database that isn't migrated to this code
        await ledger.start(routes)  # loads prices; refuses to start if a route's model has none
        try:
            yield
        finally:
            if pool is not None:
                await pool.close()

    app = FastAPI(title="ai-gateway", docs_url=None, redoc_url=None, lifespan=lifespan)

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

    async def authenticate(request: Request) -> keys.Team:
        key = keys.bearer_key(request.headers.get("authorization"))
        try:
            team = await key_store.team_for(key) if key else None
        except db.UNAVAILABLE:
            log.exception("key check failed: database unavailable")
            raise GatewayError(
                503, "the gateway can't check API keys right now", "service_unavailable_error", "auth_unavailable",
                headers={"retry-after": "5"},
            ) from None  # fmt: skip
        if team is None:
            raise GatewayError(401, "missing, invalid or revoked API key", "authentication_error", "invalid_api_key")
        return team

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        """Ready for traffic: Kubernetes sends requests only to replicas that say yes (/healthz only says the process
        is alive). Without a database there's nothing to check."""
        if pool is None:
            return {"status": "ready", "database": "not used"}
        try:
            await db.ping(pool)
        except db.UNAVAILABLE:
            return JSONResponse({"status": "not ready", "database": "unreachable"}, status_code=503)
        return {"status": "ready", "database": "ok"}

    @app.get("/v1/models")
    async def list_models(request: Request):
        team = await authenticate(request)
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": 0, "owned_by": "ai-gateway"}
                for name in routes
                if team.may_use(name)
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(body: schemas.ChatRequest, request: Request):
        team = await authenticate(request)
        route = routes.get(body.model)
        if route is None:
            raise GatewayError(
                404, f"unknown model {body.model!r}; GET /v1/models lists the routes", "invalid_request_error",
                "model_not_found", param="model",
            )  # fmt: skip
        if not team.may_use(body.model):
            raise GatewayError(
                403, f"team {team.name!r} isn't allowed to use the {body.model!r} route", "permission_error",
                "route_not_allowed", param="model",
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

        try:  # the message checks don't depend on the candidate: fail before any call
            to_provider_request(body, route.candidates[0].model, limit)
        except InvalidMessages as error:
            raise GatewayError(400, str(error), "invalid_request_error", "invalid_messages", "messages") from None
        guard = effective_guardrails(route, team)

        # Rate limits and the budget hold (accounting.py), before any provider, or the injection judge, is called.
        try:
            hold = await ledger.admit(
                team, body.model, route, body, request.state.request_id, judged=guard.injection_judge
            )
        except accounting.Limited as limited:
            headers = {"retry-after": str(max(1, math.ceil(limited.retry_after)))} if limited.retry_after else None
            type_ = "rate_limit_error" if limited.status == 429 else "invalid_request_error"
            raise GatewayError(limited.status, limited.message, type_, limited.code, headers=headers) from None
        except db.UNAVAILABLE:
            log.exception("admission failed: database unavailable")
            raise GatewayError(
                503, "the gateway can't check limits right now", "service_unavailable_error",
                "accounting_unavailable", headers={"retry-after": "5"},
            ) from None  # fmt: skip

        # Input guardrails (guardrails.py): personal data, then prompt injection. A blocked request is settled
        # (charging only the judge, if it ran) and never reaches a provider.
        try:
            checked = await guardrails.check_input(body, guard, judge)
        except guardrails.Blocked as blocked:
            await ledger.settle(
                hold, outcome="blocked", attempts=0, screen_usage=blocked.judge_usage,
                guardrails=guardrails.header(blocked.findings),
            )  # fmt: skip
            await ledger.audit_guardrails(hold, blocked.findings)
            type_ = "invalid_request_error" if blocked.status == 400 else "service_unavailable_error"
            raise GatewayError(blocked.status, blocked.message, type_, blocked.code) from None
        except BaseException:
            spawn(ledger.settle(hold, outcome="failed", attempts=0))
            raise
        safe, findings = checked.request, list(checked.findings)

        def build(candidate: config.Candidate):
            return to_provider_request(
                safe, candidate.model, limit, effort=candidate.effort, refusal_fallback=candidate.refusal_fallback
            )

        settle_failed = functools.partial(
            ledger.settle, hold, screen_usage=checked.judge_usage, guardrails=guardrails.header(findings)
        )
        deadline = asyncio.get_running_loop().time() + route.timeout.total
        try:
            if body.stream:
                # Waits for the first event, so an error before any output can still move on to the next candidate
                # or be a proper HTTP status. After the first byte the status is fixed at 200 (decision 10).
                (first, events), result = await router.stream(route, providers, breakers, build, deadline, policy)
            else:
                completion, result = await router.complete(route, providers, breakers, build, deadline, policy)
        except router.AllFailed as failure:
            log_attempts(request, team.name, body.model, failure.attempts, "failed")
            await settle_failed(outcome="failed", attempts=len(failure.attempts))
            await ledger.audit_guardrails(hold, findings)
            raise from_all_failed(failure) from None
        except BaseException:  # cancelled (the client went away) or a bug: still release the hold
            spawn(settle_failed(outcome="failed", attempts=0))
            raise

        log_attempts(request, team.name, body.model, result.attempts, "ok")
        settle = functools.partial(
            ledger.settle, hold, attempts=len(result.attempts), provider=result.candidate.provider,
            configured_model=result.candidate.model, screen_usage=checked.judge_usage,
            guardrails=guardrails.header(findings),
        )  # fmt: skip
        headers = {
            "x-gateway-route": body.model,
            "x-gateway-provider": result.candidate.provider,
            "x-gateway-attempts": str(len(result.attempts)),
            "x-gateway-fallback": "true" if result.candidate != route.candidates[0] else "false",
        }

        if not body.stream:
            output = guardrails.check_output(completion.text, guard)
            if output.findings:
                findings += output.findings
                finish = "content_filter" if output.withheld else completion.finish_reason
                completion = dataclasses.replace(completion, text=output.text, finish_reason=finish)
            await settle(outcome="ok", model=completion.model, usage=completion.usage,
                         guardrails=guardrails.header(findings))  # fmt: skip
            await ledger.audit_guardrails(hold, findings)
            if findings:
                headers["x-gateway-guardrails"] = guardrails.header(findings)
            return JSONResponse(schemas.completion_body(schemas.completion_id(), completion), headers=headers)

        if guard.buffers_output:
            # Decision 14: an answer that may be redacted or withheld is collected and checked before any of it is
            # sent. The caller waits for the whole answer; nothing unchecked is shown.
            try:
                text, finish_event = await collect(first, events, deadline)
            except (ProviderError, TimeoutError, StopAsyncIteration) as error:
                await settle(outcome="stream_error")
                await ledger.audit_guardrails(hold, findings)
                raise stream_failure(error) from None
            output = guardrails.check_output(text, guard)
            findings += output.findings
            finish = "content_filter" if output.withheld else finish_event.finish_reason
            replay = [TextDelta(output.text)] if output.text else []
            replay.append(Finish(finish, finish_event.model, finish_event.usage))
            first, events = replay[0], aiter_of(replay[1:])
            settle = functools.partial(settle, guardrails=guardrails.header(findings))
            review = None
        else:
            review = functools.partial(review_streamed, guard=guard, findings=findings, ledger=ledger, hold=hold)
        if findings:
            headers["x-gateway-guardrails"] = guardrails.header(findings)
            spawn(ledger.audit_guardrails(hold, list(findings)))
        include_usage = bool(body.stream_options and body.stream_options.include_usage)
        return StreamingResponse(
            sse(first, events, deadline, result.candidate.model, include_usage, hold, settle, review),
            media_type="text/event-stream",
            headers={**headers, "cache-control": "no-cache"},
        )

    app.state.breakers = breakers
    app.state.providers = providers
    return app


def log_attempts(request: Request, team: str, route: str, attempts: list[router.Attempt], outcome: str) -> None:
    tried = ", ".join(f"{a.provider}/{a.model}={a.outcome}" for a in attempts) or "none"
    log.info("request %s team=%s route=%s %s attempts: %s", request.state.request_id, team, route, outcome, tried)


def from_all_failed(failure: router.AllFailed) -> GatewayError:
    """What the caller sees when no candidate answered."""
    error = failure.error
    if error is not None and error.kind in router.STOP:
        return from_provider_error(error)  # the request itself was refused: say why, as the provider did
    tried = [a for a in failure.attempts if a.outcome != "skipped"]
    summary = "; ".join(f"{a.provider}/{a.model}: {a.outcome}" for a in failure.attempts)
    if not tried:
        return GatewayError(
            503, f"every candidate is paused after repeated failures ({summary})", "service_unavailable_error",
            "all_candidates_paused", headers={"retry-after": "30"},
        )  # fmt: skip
    if failure.timed_out or all(a.outcome == "timeout" for a in tried):
        return GatewayError(504, f"no candidate answered in time ({summary})", "timeout_error", "timeout")
    headers = {"retry-after": str(int(error.retry_after))} if error and error.retry_after else None
    return GatewayError(
        503, f"every candidate failed ({summary})", "service_unavailable_error", "all_candidates_failed",
        headers=headers,
    )  # fmt: skip


_background: set[asyncio.Task] = set()


def spawn(coroutine) -> None:
    """Run a coroutine that must finish even if the request that started it is cancelled (keeping a reference so
    the task isn't garbage-collected mid-way)."""
    task = asyncio.get_running_loop().create_task(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)


def effective_guardrails(route: config.Route, team: keys.Team) -> config.Guardrails:
    """The route's guardrails, tightened by the team's own settings where they're stricter."""
    if not team.guardrails:
        return route.guardrails
    try:
        return route.guardrails.strictest(config.Guardrails.model_validate(team.guardrails))
    except ValueError:  # bad settings stored for the team: the route's floor still applies
        log.error("team %s has invalid guardrail settings; using the route's", team.name)
        return route.guardrails


async def collect(first: StreamEvent, events: AsyncIterator[StreamEvent], deadline: float) -> tuple[str, Finish]:
    """A whole stream, before any of it is sent: its text and its Finish event."""
    parts, event = [], first
    try:
        while not isinstance(event, Finish):
            parts.append(event.text)
            async with asyncio.timeout_at(deadline):
                event = await anext(events)
    finally:
        await events.aclose()
    return "".join(parts), event


async def aiter_of(items: list) -> AsyncIterator:
    for item in items:
        yield item


def stream_failure(error: Exception) -> GatewayError:
    if isinstance(error, ProviderError):
        return from_provider_error(error)
    if isinstance(error, TimeoutError):
        return GatewayError(504, "the provider didn't finish in time", "timeout_error", "timeout")
    return GatewayError(502, "the provider's stream ended without finishing", "api_error", "incomplete_stream")


async def review_streamed(text: str, *, guard, findings, ledger, hold) -> str:
    """For a stream sent as it came (output only flagged): check the finished answer, audit any new finding, and
    return the guardrails summary for the usage row. The answer is already out; this records, it can't change it."""
    output = guardrails.check_output(text, guard)
    if output.findings:
        await ledger.audit_guardrails(hold, output.findings)
    return guardrails.header([*findings, *output.findings])


async def sse(
    first: StreamEvent,
    events: AsyncIterator[StreamEvent],
    deadline: float,
    model: str,
    include_usage: bool,
    hold: accounting.Hold,
    settle,
    review=None,
) -> AsyncIterator[str]:
    """Server-sent events in OpenAI's chunk format, ending with `data: [DONE]`. Settles the request's hold when the
    stream finishes, breaks, or the client goes away. `review`, if given, sees the whole text at the end."""
    cid, created = schemas.completion_id(), int(time.time())

    def data(body: dict) -> str:
        return f"data: {json.dumps(body, separators=(',', ':'))}\n\n"

    event, sent = first, []
    try:
        yield data(schemas.chunk_body(cid, created, model, {"role": "assistant", "content": ""}))
        while True:
            if isinstance(event, TextDelta):
                sent.append(event.text)
                yield data(schemas.chunk_body(cid, created, model, {"content": event.text}))
            elif isinstance(event, Finish):
                extra = {"guardrails": await review("".join(sent))} if review else {}
                await settle(outcome="ok", model=event.model, usage=event.usage, **extra)
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
        if not hold.settled:  # broke partway, or the client went away: usage unknown, so the hold is charged
            spawn(settle(outcome="stream_error"))
    yield data(schemas.error_body(error.message, error.type, error.code))
