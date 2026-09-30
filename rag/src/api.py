"""HTTP API for the RAG pipeline.

Run from the rag/ folder:
    venv\\Scripts\\python.exe -m uvicorn api:app --app-dir src --reload

Endpoints:
    GET  /health       liveness check (never requires a key)
    POST /ask          retrieve + generate, return the whole answer as one JSON response
    POST /ask/stream   retrieve + generate, stream the answer as Server-Sent Events (SSE)

If RAG_API_KEY is set, the /ask endpoints require it in an X-API-Key header. Leave it unset for
local development; always set it for anything reachable from the internet, since every
request spends Anthropic credits.
"""

import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import anthropic
import chromadb.errors
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import telemetry
import tracing
import vector_store
from embeddings import MODELS
from query import ANSWER_MAX_TOKENS, ANSWER_MODEL, ANSWER_PROMPT, answer_text, build_messages, generate, stream_answer
from retrieval import retrieve, warm_up

# uvicorn's own logger already prints to the console, so startup messages appear next to its own.
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load models and indexes before accepting traffic, so the first user doesn't wait ~1 s for
    # them. This matters most when the host scales to zero and every idle period ends in a cold start.
    if os.environ.get("RAG_WARM_UP", "true").lower() != "false":
        start = time.perf_counter()
        try:
            warm_up(rerank=os.environ.get("RAG_WARM_UP_RERANKER", "false").lower() == "true")
            logger.info("Warm-up finished in %.0f ms", (time.perf_counter() - start) * 1000)
        except Exception:
            # A missing index shouldn't stop the server from starting; the first request will
            # hit the same error and report it properly.
            logger.exception("Warm-up failed; continuing without it")
    yield
    tracing.flush()  # on shutdown, send traces still waiting in Langfuse's background batch


app = FastAPI(title="RAG API", lifespan=lifespan)

# The SDK's default timeout is 10 minutes per attempt, with 2 retries, so one stuck call could hold a
# request for half an hour. Answers here take a few seconds; a minute means something is wrong.
CLAUDE_TIMEOUT_SECONDS = 60.0


def make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(timeout=CLAUDE_TIMEOUT_SECONDS)


# One client for the whole process: it holds a connection pool, so reusing it avoids
# a new TLS handshake per request. It reads ANTHROPIC_API_KEY from the environment.
client = make_client() if os.environ.get("ANTHROPIC_API_KEY") else None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=3, ge=1, le=20)
    embedding_model: str = "default"
    hybrid: bool = False
    rerank: bool = False


class Source(BaseModel):
    id: str
    source: str
    text: str


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]
    # "end_turn" means a complete answer; "max_tokens" means it was cut off at the length limit.
    stop_reason: str


def _retrieve(req: AskRequest) -> list[dict]:
    if req.embedding_model not in MODELS:
        raise HTTPException(422, f"embedding_model must be one of {list(MODELS)}")
    try:
        return retrieve(
            req.question,
            k=req.k,
            embedding_model=req.embedding_model,
            hybrid=req.hybrid,
            use_reranker=req.rerank,
        )
    except (vector_store.VectorStoreUnavailable, httpx.TransportError):
        # The Chroma server is down or unreachable: temporary, so tell the caller to retry.
        raise HTTPException(503, "The search index is temporarily unavailable. Try again shortly.",
                            headers={"Retry-After": "10"})
    except chromadb.errors.NotFoundError:
        # A valid model name, but nobody has built its index on this server (each embedding model
        # needs its own). That's the caller's choice to fix, not a server fault, so not a 500.
        raise HTTPException(
            422,
            f"No index has been built for embedding_model '{req.embedding_model}' on this server. "
            f"Use another model, or run: python src/ingest.py --embedding-model {req.embedding_model}",
        )


def _sources(hits: list[dict]) -> list[Source]:
    # Hits also carry scores, some of which are numpy floats that JSON can't encode.
    # Clients only need to know where the answer came from.
    return [Source(id=h["id"], source=h["source"], text=h["text"]) for h in hits]


def _sse(event: str, data) -> str:
    """Format one Server-Sent Event: an event name line, a data line, then a blank line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject the request unless it carries the configured key. Read on every request, so the
    key can be rotated by restarting with a new value."""
    expected = os.environ.get("RAG_API_KEY")
    if not expected:
        return
    # compare_digest takes the same time whether the first or the last character differs, so the
    # response time can't be used to guess the key one character at a time.
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid API key", headers={"WWW-Authenticate": "X-API-Key"})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "generation_enabled": client is not None,
        "auth_required": bool(os.environ.get("RAG_API_KEY")),
        "version": os.environ.get("RAG_VERSION", "dev"),
    }


def _new_record(endpoint: str, req: AskRequest) -> dict:
    """Start the log record for one request. The question text itself is left out on purpose:
    user input can contain personal data, and logs are kept longer and seen by more people."""
    return {
        "request_id": uuid.uuid4().hex,
        "endpoint": endpoint,
        "prompt": ANSWER_PROMPT.id,
        "question_chars": len(req.question),
        "k": req.k,
        "embedding_model": req.embedding_model,
        "hybrid": req.hybrid,
        "rerank": req.rerank,
    }


def _trace_metadata(req: AskRequest) -> dict:
    return {"k": req.k, "embedding_model": req.embedding_model, "hybrid": req.hybrid, "rerank": req.rerank}


def _traced_retrieve(root, req: AskRequest) -> list[dict]:
    span = root.start_observation(name="retrieval", as_type="retriever", input=req.question)
    try:
        hits = _retrieve(req)
    except Exception as e:
        span.update(level="ERROR", status_message=str(e))
        raise
    else:
        span.update(output=[s.model_dump() for s in _sources(hits)])
        return hits
    finally:
        span.end()


def _start_generation(root, req: AskRequest, hits: list[dict]):
    return root.start_observation(
        name="answer",
        as_type="generation",
        model=ANSWER_MODEL,
        model_parameters={"max_tokens": ANSWER_MAX_TOKENS},
        version=ANSWER_PROMPT.id,  # Langfuse can filter and compare traces by this
        metadata={"prompt_sha256": ANSWER_PROMPT.sha256},
        input=build_messages(req.question, hits),
    )


def _finish_generation(gen, root, record: dict, message) -> str:
    """Copy usage and cost onto the log record and the trace, and return the answer text."""
    answer = answer_text(message)
    record.update(telemetry.usage_fields(ANSWER_MODEL, message))
    gen.update(
        output=answer,
        usage_details={"input": record["input_tokens"], "output": record["output_tokens"]},
        cost_details={"total": record["cost_usd"]} if record["cost_usd"] is not None else None,
    )
    gen.end()
    root.update(output=answer)
    root.set_trace_io(output=answer)
    return answer


def _fail(obs, message: str) -> None:
    obs.update(level="ERROR", status_message=message)


def _error_name(e: Exception) -> str:
    return f"HTTP {e.status_code}" if isinstance(e, HTTPException) else type(e).__name__


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(require_api_key)])
def ask(req: AskRequest, response: Response):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set")
    start = time.perf_counter()
    record = _new_record("/ask", req)
    response.headers["X-Request-ID"] = record["request_id"]
    root = tracing.start_trace(record["request_id"], "rag-ask", req.question, _trace_metadata(req))

    try:
        with telemetry.timed(record, "retrieval_ms"):
            hits = _traced_retrieve(root, req)
        record["n_hits"] = len(hits)

        gen = _start_generation(root, req, hits)
        try:
            with telemetry.timed(record, "generation_ms"):
                message = generate(client, req.question, hits)
        except anthropic.APIError as e:
            _fail(gen, type(e).__name__)
            gen.end()
            record.update(status="error", error=type(e).__name__)
            # 502 Bad Gateway: our server is fine, the service it depends on failed.
            raise HTTPException(502, f"Generation failed: {type(e).__name__}")

        answer = _finish_generation(gen, root, record, message)
        if message.stop_reason == "refusal":
            # Claude declined; any text is at most a fragment, so don't present it as an answer.
            record["status"] = "refused"
            root.update(level="WARNING", status_message="Claude declined to answer")
            raise HTTPException(422, "Claude declined to answer this question.")
        record["status"] = "ok"
        return AskResponse(answer=answer, sources=_sources(hits), stop_reason=message.stop_reason)
    except Exception as e:
        # Covers our own HTTP errors (422, 502, 503) and anything unexpected, which becomes a 500.
        record.setdefault("status", "error")
        record.setdefault("error", _error_name(e))
        if record["status"] == "error":
            _fail(root, record["error"])
        raise
    finally:
        # Exactly one log line per request, whatever happened above.
        record.setdefault("total_ms", telemetry.elapsed_ms(start))
        telemetry.log_request(record)
        root.end()


@app.post("/ask/stream", dependencies=[Depends(require_api_key)])
def ask_stream(req: AskRequest):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set")
    start = time.perf_counter()
    record = _new_record("/ask/stream", req)
    root = tracing.start_trace(record["request_id"], "rag-ask-stream", req.question, _trace_metadata(req))

    # Retrieve before streaming starts, so a bad request still gets a normal HTTP error.
    # Once the first byte is sent the status code is locked in at 200.
    try:
        with telemetry.timed(record, "retrieval_ms"):
            hits = _traced_retrieve(root, req)
    except Exception as e:
        record.update(status="error", error=_error_name(e), total_ms=telemetry.elapsed_ms(start))
        _fail(root, record["error"])
        telemetry.log_request(record)
        root.end()
        raise
    record["n_hits"] = len(hits)

    def events():
        # Assume the client hung up until we reach the end: if they disconnect mid-answer,
        # the generator is closed at a yield and only the finally block runs.
        record["status"] = "cancelled"
        generation_start = time.perf_counter()
        gen = _start_generation(root, req, hits)
        finished = False  # whether _finish_generation has already ended the generation span
        try:
            yield _sse("sources", [s.model_dump() for s in _sources(hits)])
            for item in stream_answer(client, req.question, hits):
                if isinstance(item, str):
                    if "ttft_ms" not in record:
                        # Time to first token: how long the user stares at nothing before text appears.
                        record["ttft_ms"] = telemetry.elapsed_ms(start)
                        # Langfuse derives its own time-to-first-token from this timestamp.
                        gen.update(completion_start_time=datetime.now(timezone.utc))
                    yield _sse("token", item)
                else:
                    record["generation_ms"] = telemetry.elapsed_ms(generation_start)
                    _finish_generation(gen, root, record, item)
                    finished = True
                    # The client sees stop_reason in the done event: "max_tokens" means the answer
                    # was cut off, "refusal" means Claude declined partway.
                    record.update(
                        status="refused" if item.stop_reason == "refusal" else "ok",
                        total_ms=telemetry.elapsed_ms(start),
                    )
                    done = {k: record.get(k) for k in (
                        "stop_reason", "input_tokens", "output_tokens", "cost_usd",
                        "retrieval_ms", "ttft_ms", "total_ms",
                    )}
                    yield _sse("done", done)
        except anthropic.APIError as e:
            record.update(status="error", error=type(e).__name__)
            _fail(gen, type(e).__name__)
            _fail(root, f"Generation failed: {type(e).__name__}")
            # Too late for an HTTP error status, so report the failure as an event instead.
            yield _sse("error", {"type": type(e).__name__, "message": str(e)})
        finally:
            if record["status"] == "cancelled":
                root.update(level="WARNING", status_message="client disconnected mid-answer")
            if not finished:
                gen.end()
            root.end()
            record.setdefault("total_ms", telemetry.elapsed_ms(start))
            telemetry.log_request(record)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"X-Request-ID": record["request_id"]}
    )
