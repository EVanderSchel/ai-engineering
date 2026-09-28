"""HTTP API for the RAG pipeline.

Run from the rag/ folder:
    venv\\Scripts\\python.exe -m uvicorn api:app --app-dir src --reload

Endpoints:
    GET  /health       liveness check
    POST /ask          retrieve + generate, return the whole answer as one JSON response
    POST /ask/stream   retrieve + generate, stream the answer as Server-Sent Events (SSE)
"""

import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import anthropic
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import telemetry
import tracing
from embeddings import MODELS
from query import ANSWER_MAX_TOKENS, ANSWER_MODEL, ANSWER_PROMPT, build_messages, generate, stream_answer
from retrieval import retrieve


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    tracing.flush()  # on shutdown, send traces still waiting in Langfuse's background batch


app = FastAPI(title="RAG API", lifespan=lifespan)

# One client for the whole process: it holds a connection pool, so reusing it avoids
# a new TLS handshake per request. It reads ANTHROPIC_API_KEY from the environment.
client = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None


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


def _retrieve(req: AskRequest) -> list[dict]:
    if req.embedding_model not in MODELS:
        raise HTTPException(422, f"embedding_model must be one of {list(MODELS)}")
    return retrieve(
        req.question,
        k=req.k,
        embedding_model=req.embedding_model,
        hybrid=req.hybrid,
        use_reranker=req.rerank,
    )


def _sources(hits: list[dict]) -> list[Source]:
    # Hits also carry scores, some of which are numpy floats that JSON can't encode.
    # Clients only need to know where the answer came from.
    return [Source(id=h["id"], source=h["source"], text=h["text"]) for h in hits]


def _sse(event: str, data) -> str:
    """Format one Server-Sent Event: an event name line, a data line, then a blank line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/health")
def health():
    return {"status": "ok", "generation_enabled": client is not None}


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
    answer = message.content[0].text
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


@app.post("/ask", response_model=AskResponse)
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
            _fail(root, f"Generation failed: {type(e).__name__}")
            record.update(status="error", error=type(e).__name__, total_ms=telemetry.elapsed_ms(start))
            telemetry.log_request(record)
            # 502 Bad Gateway: our server is fine, the service it depends on failed.
            raise HTTPException(502, f"Generation failed: {type(e).__name__}")

        answer = _finish_generation(gen, root, record, message)
        record.update(status="ok", total_ms=telemetry.elapsed_ms(start))
        telemetry.log_request(record)
        return AskResponse(answer=answer, sources=_sources(hits))
    finally:
        root.end()


@app.post("/ask/stream")
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
    except Exception:
        root.end()
        raise
    record["n_hits"] = len(hits)

    def events():
        # Assume the client hung up until we reach the end: if they disconnect mid-answer,
        # the generator is closed at a yield and only the finally block runs.
        record["status"] = "cancelled"
        generation_start = time.perf_counter()
        gen = _start_generation(root, req, hits)
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
                    record.update(status="ok", total_ms=telemetry.elapsed_ms(start))
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
            if record["status"] != "ok":
                gen.end()  # on success _finish_generation already ended it
            root.end()
            record.setdefault("total_ms", telemetry.elapsed_ms(start))
            telemetry.log_request(record)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"X-Request-ID": record["request_id"]}
    )
