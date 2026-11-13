"""A fake OpenAI-compatible model server (step 10): the one upstream both gateways call when they're compared, so
any difference measured is the gateways', not their mocks'. Free, offline, and the same for both.

    uv run uvicorn fake_upstream:app --app-dir src --port 8090
    docker compose --profile compare up -d fake-upstream      # in compose, from the gateway's image

The model name picks the behaviour, so a gateway's routes or model list can stage each case:
- `fake-ok` (or anything not below): answers FAKE_WORDS words ("word1 word2 ..."), the first after FAKE_FIRST_TOKEN_MS
  and each next one FAKE_WORD_MS later, like a model producing tokens;
- `fake-503`: 503, as an overloaded provider answers (both gateways should retry, then fall back);
- `fake-429`: 429 with retry-after: 1;
- `fake-400`: 400, a request the provider refuses (both gateways should pass it on, not fall back);
- `fake-slow`: the first word after 60 s (for timeouts).

A plain (not streamed) request answers in the shape it asks for, so clients' structured features can be followed end to
end (stage 4): with `tools`, a call to the first tool; with a `response_format` JSON schema, JSON matching it. The
values are placeholders built from the schema ("x", 0, false), the way a test double fills a form.

Plain language: a stand-in kitchen that cooks every order the same way at the same speed, so two waiters can be
compared on how fast they carry the plates.
"""

import asyncio
import collections
import itertools
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

WORDS = int(os.environ.get("FAKE_WORDS", "20"))
FIRST_TOKEN = int(os.environ.get("FAKE_FIRST_TOKEN_MS", "100")) / 1000
PER_WORD = int(os.environ.get("FAKE_WORD_MS", "5")) / 1000
SLOW_FIRST_TOKEN = 60.0

app = FastAPI(title="fake-upstream", docs_url=None, redoc_url=None)
ids = itertools.count(1)
counts: collections.Counter = collections.Counter()  # calls per model: how often a gateway retried or fell back


def error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    body = {"error": {"message": message, "type": "fake_error", "code": str(status)}}
    return JSONResponse(body, status_code=status, headers=headers)


def prompt_tokens(body: dict) -> int:
    texts = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):  # content parts
            texts += [part.get("text", "") for part in content if isinstance(part, dict)]
        elif content:
            texts.append(str(content))
    return sum(len(text.split()) for text in texts)


def placeholder(schema: dict):
    """A value matching a JSON schema's shape: every property filled, every type given its simplest value."""
    kind = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if kind == "object" or "properties" in schema:
        return {name: placeholder(sub) for name, sub in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [placeholder(schema.get("items") or {})]
    return {"integer": 0, "number": 0, "boolean": False, "null": None}.get(kind, "x")


def shaped(body: dict) -> tuple[dict, str] | None:
    """The message for a request that asks for a tool call or JSON, with its finish reason; None for plain text."""
    if body.get("tools"):
        function = body["tools"][0]["function"]
        call = {"id": f"call_fake_{next(ids)}", "type": "function",
                "function": {"name": function["name"],
                             "arguments": json.dumps(placeholder(function.get("parameters") or {}))}}  # fmt: skip
        return {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
    response_format = body.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        content = json.dumps(placeholder(response_format["json_schema"].get("schema") or {}))
        return {"role": "assistant", "content": content}, "stop"
    return None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/stats")
async def stats() -> dict:
    """Calls so far, per model name. The parity suite reads it before and after a case."""
    return dict(counts)


@app.get("/v1/models")
async def models() -> dict:
    names = ["fake-ok", "fake-503", "fake-429", "fake-400", "fake-slow"]
    return {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "fake"} for n in names]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model") or "fake-ok"
    counts[model] += 1
    if model == "fake-503":
        return error(503, "the fake upstream is overloaded")
    if model == "fake-429":
        return error(429, "the fake upstream's rate limit", {"retry-after": "1"})
    if model == "fake-400":
        return error(400, "the fake upstream refuses this request")
    wanted = body.get("max_tokens") or body.get("max_completion_tokens") or WORDS
    words = [f"word{i}" for i in range(1, min(WORDS, int(wanted)) + 1)]
    finish = "stop" if len(words) == WORDS else "length"
    usage = {"prompt_tokens": prompt_tokens(body), "completion_tokens": len(words),
             "total_tokens": prompt_tokens(body) + len(words)}  # fmt: skip
    first = SLOW_FIRST_TOKEN if model == "fake-slow" else FIRST_TOKEN
    cid, created = f"chatcmpl-fake-{next(ids)}", int(time.time())

    if not body.get("stream"):
        await asyncio.sleep(first + PER_WORD * max(len(words) - 1, 0))
        message, finish = shaped(body) or ({"role": "assistant", "content": " ".join(words)}, finish)
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
        }  # fmt: skip

    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    def chunk(delta: dict, finish_reason=None) -> str:
        data = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}  # fmt: skip
        return f"data: {json.dumps(data)}\n\n"

    async def events():
        yield chunk({"role": "assistant", "content": ""})
        await asyncio.sleep(first)
        for i, word in enumerate(words):
            if i:
                await asyncio.sleep(PER_WORD)
            yield chunk({"content": word if i == 0 else f" {word}"})
        yield chunk({}, finish)
        if include_usage:
            data = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [], "usage": usage}  # fmt: skip
            yield f"data: {json.dumps(data)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
