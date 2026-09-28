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

import anthropic
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import telemetry
from embeddings import MODELS
from query import ANSWER_MODEL, generate, stream_answer
from retrieval import retrieve

app = FastAPI(title="RAG API")

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
        "question_chars": len(req.question),
        "k": req.k,
        "embedding_model": req.embedding_model,
        "hybrid": req.hybrid,
        "rerank": req.rerank,
    }


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, response: Response):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set")
    start = time.perf_counter()
    record = _new_record("/ask", req)
    response.headers["X-Request-ID"] = record["request_id"]

    with telemetry.timed(record, "retrieval_ms"):
        hits = _retrieve(req)
    record["n_hits"] = len(hits)

    try:
        with telemetry.timed(record, "generation_ms"):
            message = generate(client, req.question, hits)
    except anthropic.APIError as e:
        record.update(status="error", error=type(e).__name__, total_ms=telemetry.elapsed_ms(start))
        telemetry.log_request(record)
        # 502 Bad Gateway: our server is fine, the service it depends on failed.
        raise HTTPException(502, f"Generation failed: {type(e).__name__}")

    record.update(telemetry.usage_fields(ANSWER_MODEL, message))
    record.update(status="ok", total_ms=telemetry.elapsed_ms(start))
    telemetry.log_request(record)
    return AskResponse(answer=message.content[0].text, sources=_sources(hits))


@app.post("/ask/stream")
def ask_stream(req: AskRequest):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set")
    start = time.perf_counter()
    record = _new_record("/ask/stream", req)

    # Retrieve before streaming starts, so a bad request still gets a normal HTTP error.
    # Once the first byte is sent the status code is locked in at 200.
    with telemetry.timed(record, "retrieval_ms"):
        hits = _retrieve(req)
    record["n_hits"] = len(hits)

    def events():
        # Assume the client hung up until we reach the end: if they disconnect mid-answer,
        # the generator is closed at a yield and only the finally block runs.
        record["status"] = "cancelled"
        generation_start = time.perf_counter()
        try:
            yield _sse("sources", [s.model_dump() for s in _sources(hits)])
            for item in stream_answer(client, req.question, hits):
                if isinstance(item, str):
                    # Time to first token: how long the user stares at nothing before text appears.
                    record.setdefault("ttft_ms", telemetry.elapsed_ms(start))
                    yield _sse("token", item)
                else:
                    record["generation_ms"] = telemetry.elapsed_ms(generation_start)
                    record.update(telemetry.usage_fields(ANSWER_MODEL, item))
                    record.update(status="ok", total_ms=telemetry.elapsed_ms(start))
                    done = {k: record.get(k) for k in (
                        "stop_reason", "input_tokens", "output_tokens", "cost_usd",
                        "retrieval_ms", "ttft_ms", "total_ms",
                    )}
                    yield _sse("done", done)
        except anthropic.APIError as e:
            record.update(status="error", error=type(e).__name__)
            # Too late for an HTTP error status, so report the failure as an event instead.
            yield _sse("error", {"type": type(e).__name__, "message": str(e)})
        finally:
            record.setdefault("total_ms", telemetry.elapsed_ms(start))
            telemetry.log_request(record)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"X-Request-ID": record["request_id"]}
    )
