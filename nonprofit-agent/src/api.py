"""HTTP API: ask the nonprofit research agent a question, get its answer and how it got there.

Run from the nonprofit-agent/ folder:
    .venv\\Scripts\\python.exe -m uvicorn api:app --app-dir src --reload
then open http://localhost:8000/docs to try it.

Endpoints:
    GET  /health   liveness check and version (never requires a key)
    POST /ask      {"question": "..."}: the agent's answer, why it stopped if it didn't answer, figures the
                   grounding guardrail couldn't verify, the tool calls it made, and the cost

Every question spends Anthropic credit (about $0.005-0.15, capped at $0.25), so a public deployment needs
the checks below, applied in order from cheapest to most expensive so a rejected request never reaches
Claude:
    1. API key (NONPROFIT_API_KEY, compared in constant time; unset only for local development)
    2. question length (3-500 characters)
    3. per-key rate limit (rate_limit.py, default 5 questions a minute)
    4. daily spend cap (spend.py, default $2.00 a UTC day)
    5. one question at a time per container (another arriving meanwhile gets 503 and Retry-After)
    6. the agent's own limits (10 model calls, $0.25), fixed here: a caller can't raise them
plus the hard backstop outside this code: the deployment's key belongs to a dedicated Anthropic workspace
with its own spend limit.
"""

import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid

import anthropic
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

import agent
import rate_limit
import spend

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="Nonprofit research agent API")

# A question takes 5-60 seconds in total; one model call over two minutes means something is wrong.
CLAUDE_TIMEOUT_SECONDS = 120.0
BUSY_RETRY_SECONDS = 30

# One client for the whole process (it keeps a connection pool). It reads ANTHROPIC_API_KEY.
client = anthropic.Anthropic(timeout=CLAUDE_TIMEOUT_SECONDS) if os.environ.get("ANTHROPIC_API_KEY") else None
_one_at_a_time = threading.Semaphore(1)


# --- Request checks --------------------------------------------------------------------------------


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject the request unless it carries the configured key, compared in constant time so response
    times can't reveal it a character at a time. Read per request, so a restart rotates it."""
    expected = os.environ.get("NONPROFIT_API_KEY")
    if not expected:
        return
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid API key", headers={"WWW-Authenticate": "X-API-Key"})


def enforce_rate_limit(request: Request) -> None:
    """After the key check (so it counts real callers) and before anything costs money."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    client_ip = forwarded or (request.client.host if request.client else None)
    retry_after = rate_limit.check(rate_limit.caller_id(bool(os.environ.get("NONPROFIT_API_KEY")), client_ip))
    if retry_after is not None:
        raise HTTPException(
            429, "Too many questions. Try again shortly.", headers={"Retry-After": str(max(1, round(retry_after)))}
        )


ASK_DEPENDENCIES = [Depends(require_api_key), Depends(enforce_rate_limit)]


# --- Requests and responses ------------------------------------------------------------------------


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500, description="A question about US nonprofits")


class ToolCall(BaseModel):
    name: str
    input: dict
    is_error: bool


class AskResponse(BaseModel):
    request_id: str
    answer: str | None  # null when the agent stopped without answering: see explanation
    stop: str  # "answered", or why it stopped: max_steps, max_cost, max_tokens, refusal
    explanation: str | None
    unverified_figures: list[str]  # figures in the answer that no tool returned (left after one retry)
    tool_calls: list[ToolCall]  # what the agent looked up, so the answer can be checked
    model_calls: int
    cost_usd: float
    seconds: float
    model: str
    version: str


class ErrorResponse(BaseModel):
    detail: str


ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing or wrong X-API-Key (only when NONPROFIT_API_KEY is set)"},
    422: {"description": "The question is missing, shorter than 3 or longer than 500 characters"},
    429: {"model": ErrorResponse, "description": "Rate limit or daily spend cap reached (see Retry-After)"},
    502: {"model": ErrorResponse, "description": "The Claude API failed (see detail)"},
    503: {"model": ErrorResponse, "description": "Busy with another question, or no Anthropic key configured"},
}


def version() -> str:
    return os.environ.get("NONPROFIT_VERSION", "dev")


# --- Endpoints -------------------------------------------------------------------------------------


@app.get("/health")
def health():
    return {
        "status": "ok",
        "answering_enabled": client is not None,
        "auth_required": bool(os.environ.get("NONPROFIT_API_KEY")),
        "model": agent.MODEL,
        "daily_cap_usd": spend.daily_cap(),
        "version": version(),
    }


@app.post("/ask", response_model=AskResponse, responses=ERROR_RESPONSES, dependencies=ASK_DEPENDENCIES)
def ask(body: AskRequest):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set on the server")
    if not spend.can_start(agent.MAX_COST):
        raise HTTPException(
            429,
            f"Today's spend cap (${spend.daily_cap():.2f}) is reached. Try again after midnight UTC.",
            headers={"Retry-After": str(spend.seconds_until_reset())},
        )
    if not _one_at_a_time.acquire(blocking=False):
        raise HTTPException(
            503, "Busy answering another question. Try again shortly.", headers={"Retry-After": str(BUSY_RETRY_SECONDS)}
        )
    request_id, started = uuid.uuid4().hex[:12], time.monotonic()
    try:
        result = agent.run(client, body.question)  # the agent's own step and cost limits apply
    finally:
        _one_at_a_time.release()
    spend.record(result.cost)
    seconds = round(time.monotonic() - started, 1)
    _log(request_id, body.question, result, seconds)

    if result.stop == "api_error":
        raise HTTPException(502, result.error)
    return AskResponse(
        request_id=request_id,
        answer=result.answer,
        stop=result.stop,
        explanation=agent.explain_stop(result),
        unverified_figures=result.ungrounded,
        tool_calls=[
            ToolCall(name=e["name"], input=e["input"], is_error=e["is_error"])
            for e in result.trajectory
            if e["type"] == "tool"
        ],
        model_calls=result.steps,
        cost_usd=round(result.cost, 5),
        seconds=seconds,
        model=result.served_models[-1] if result.served_models else agent.MODEL,
        version=version(),
    )


def _log(request_id: str, question: str, result: agent.Result, seconds: float) -> None:
    """One JSON line per question, for the host's log search. Never the question text (it's the caller's
    content): its length and a short hash, so repeats can be spotted without storing what was asked."""
    line = {
        "event": "ask",
        "request_id": request_id,
        "question_chars": len(question),
        "question_hash": hashlib.sha256(question.encode()).hexdigest()[:12],
        "stop": result.stop,
        "model_calls": result.steps,
        "tool_calls": sum(e["type"] == "tool" for e in result.trajectory),
        "grounding_retries": result.grounding_retries,
        "cost_usd": round(result.cost, 5),
        "spent_today_usd": round(spend.spent_today(), 4),
        "seconds": seconds,
    }
    logger.info(json.dumps(line))
