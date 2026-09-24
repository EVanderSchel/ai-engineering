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

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from embeddings import MODELS
from query import stream_answer, synthesize_answer
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


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set")
    hits = _retrieve(req)
    return AskResponse(answer=synthesize_answer(req.question, hits), sources=_sources(hits))


@app.post("/ask/stream")
def ask_stream(req: AskRequest):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set")
    # Retrieve before streaming starts, so a bad request still gets a normal HTTP error.
    # Once the first byte is sent the status code is locked in at 200.
    hits = _retrieve(req)

    def events():
        yield _sse("sources", [s.model_dump() for s in _sources(hits)])
        try:
            for item in stream_answer(client, req.question, hits):
                if isinstance(item, str):
                    yield _sse("token", item)
                else:
                    yield _sse(
                        "done",
                        {
                            "stop_reason": item.stop_reason,
                            "input_tokens": item.usage.input_tokens,
                            "output_tokens": item.usage.output_tokens,
                        },
                    )
        except anthropic.APIError as e:
            # Too late for an HTTP error status, so report the failure as an event instead.
            yield _sse("error", {"type": type(e).__name__, "message": str(e)})

    return StreamingResponse(events(), media_type="text/event-stream")
