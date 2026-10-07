"""HTTP API: send a Form 990, get back Part I as structured data, with a say on whether to trust it.

Run from the irs-990-extraction/ folder:
    .venv\\Scripts\\python.exe -m uvicorn api:app --app-dir src --reload
then open http://localhost:8000/docs to try it.

Endpoints:
    GET  /health    liveness check (never requires a key)
    POST /extract   upload a return (PDF, or an image of page 1); returns the 42 Part I fields, the form's
                    arithmetic checks, and a review recommendation (step 6's signals that work on a single
                    request: no answer, failed checks or a retry, or a long think on a hard page)

If IRS990_API_KEY is set, /extract requires it in an X-API-Key header. Leave it unset for local
development; always set it for anything reachable from the internet, since every request spends
Anthropic credits.
"""

import io
import json
import logging
import os
import secrets
import time
import uuid
from typing import Annotated

import anthropic
import pymupdf
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

import extract
import rate_limit
import review
from extract import MODEL, Extraction
from fields import ALL_FIELDS

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="Form 990 extraction API")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # an IRS return PDF is typically 1-15 MB
LONG_EDGE_PX = extract.check_input(extract.INPUT)  # 1568 px: the size step 4 chose
# The SDK's default timeout is 10 minutes per attempt; extraction takes about 10 seconds, a hard page
# a minute or two, so two minutes means something is wrong.
CLAUDE_TIMEOUT_SECONDS = 120.0
# The step 6 rule, minus the second run (which doubles the cost): review the whole document when the
# checks failed or needed a retry, or Claude thought for more than 2,000 tokens.
REVIEW_RULE = review.Rule("checks + long thinking (> 2,000 tokens)", max_output_tokens=2000, checks=True)

# One client for the whole process: it holds a connection pool, so requests don't each pay a new TLS
# handshake. It reads ANTHROPIC_API_KEY from the environment.
client = anthropic.Anthropic(timeout=CLAUDE_TIMEOUT_SECONDS) if os.environ.get("ANTHROPIC_API_KEY") else None


# --- Request checks --------------------------------------------------------------------------------


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject the request unless it carries the configured key, compared in constant time so response
    times can't reveal it a character at a time. Read per request, so a restart rotates it."""
    expected = os.environ.get("IRS990_API_KEY")
    if not expected:
        return
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid API key", headers={"WWW-Authenticate": "X-API-Key"})


def enforce_rate_limit(request: Request) -> None:
    """After the key check (so it counts real callers) and before the upload is read or Claude is
    called (so a rejected request costs nothing)."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    client_ip = forwarded or (request.client.host if request.client else None)
    retry_after = rate_limit.check(rate_limit.caller_id(bool(os.environ.get("IRS990_API_KEY")), client_ip))
    if retry_after is not None:
        raise HTTPException(
            429, "Too many requests. Try again shortly.", headers={"Retry-After": str(max(1, round(retry_after)))}
        )


EXTRACT_DEPENDENCIES = [Depends(require_api_key), Depends(enforce_rate_limit)]


# --- Responses -------------------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    detail: str


class Review(BaseModel):
    needed: bool  # send this extraction to a person before trusting it
    reasons: list[str]


class ExtractResponse(BaseModel):
    request_id: str
    fields: dict  # the 42 Part I fields; null means the line is blank on the form (not 0)
    problems: list[str]  # the form's arithmetic rules the answer breaks (empty: all passed)
    review: Review
    model: str
    prompt: str
    attempts: int
    cost_usd: float
    seconds: float


ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing or wrong X-API-Key (only when IRS990_API_KEY is set)"},
    413: {"model": ErrorResponse, "description": f"Upload larger than {MAX_UPLOAD_BYTES // 2**20} MB"},
    415: {"model": ErrorResponse, "description": "Not a PDF, PNG, or JPEG"},
    422: {"description": "Unreadable file, a page the PDF doesn't have, or Claude gave no answer"},
    429: {"model": ErrorResponse, "description": "Too many requests from this caller (see the Retry-After header)"},
    502: {"model": ErrorResponse, "description": "The Claude API call failed"},
    503: {"model": ErrorResponse, "description": "No Anthropic key configured on the server"},
}


# --- Turning an upload into page 1 ----------------------------------------------------------------


def page_image(data: bytes, page: int) -> dict:
    """The uploaded page as the image block extract() sends: a PDF page rendered at 1568 px, or an
    uploaded image scaled to the same size. Raises HTTPException for anything unusable."""
    if data.startswith(b"%PDF"):
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if not 1 <= page <= doc.page_count:
                    raise HTTPException(422, f"page {page} doesn't exist: the PDF has {doc.page_count} page(s)")
                pdf_page = doc[page - 1]
                zoom = LONG_EDGE_PX / max(pdf_page.rect.width, pdf_page.rect.height)
                png = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
        except (pymupdf.FileDataError, RuntimeError) as e:
            raise HTTPException(422, "The PDF couldn't be read") from e
    elif data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff"):
        if page != 1:
            raise HTTPException(422, "An image upload is a single page: leave page at 1")
        try:
            image = Image.open(io.BytesIO(data))
            image.thumbnail((LONG_EDGE_PX, LONG_EDGE_PX))  # scales down only, keeps the aspect
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, "PNG")
            png = buffer.getvalue()
        except (UnidentifiedImageError, OSError) as e:
            raise HTTPException(422, "The image couldn't be read") from e
    else:
        raise HTTPException(415, "Upload a PDF, PNG, or JPEG of the return")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": extract._base64(png)}}


def review_of(result: Extraction) -> Review:
    record = {"answer": result.answer, "problems": result.problems, "attempts": result.attempts}
    record["usage"] = {"output_tokens": result.usage.output_tokens}
    if not review.flagged_fields(record, REVIEW_RULE):
        return Review(needed=False, reasons=[])
    reasons = []
    if not result.answer:
        reasons.append("no answer")
    if result.problems:
        reasons.append("the form's arithmetic doesn't add up")
    elif len(result.attempts) > 1:
        reasons.append("the arithmetic only added up on a retry")
    if result.usage.output_tokens > REVIEW_RULE.max_output_tokens:
        reasons.append(f"a hard page (Claude wrote {result.usage.output_tokens:,} tokens)")
    return Review(needed=True, reasons=reasons)


# --- Endpoints -------------------------------------------------------------------------------------


@app.get("/health")
def health():
    return {
        "status": "ok",
        "extraction_enabled": client is not None,
        "auth_required": bool(os.environ.get("IRS990_API_KEY")),
        "model": MODEL,
        "version": os.environ.get("IRS990_VERSION", "dev"),
    }


@app.post("/extract", response_model=ExtractResponse, responses=ERROR_RESPONSES, dependencies=EXTRACT_DEPENDENCIES)
def extract_return(
    file: Annotated[UploadFile, File(description="The return as a PDF, or an image of page 1 (PNG or JPEG)")],
    page: int = Query(1, ge=1, description="The page holding Part I (page 1 on every IRS rendering)"),
):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set on the server")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"The upload is over {MAX_UPLOAD_BYTES // 2**20} MB")
    block = page_image(data, page)

    request_id, started = uuid.uuid4().hex[:12], time.monotonic()
    try:
        result = extract.extract(client, block, cache=True)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        _log(request_id, 502, started, error=type(e).__name__)
        raise HTTPException(502, f"Extraction failed: {type(e).__name__}") from e
    seconds = round(time.monotonic() - started, 1)
    if result.answer is None:
        _log(request_id, 422, started, cost=result.cost_usd, error=result.problems[0])
        raise HTTPException(422, f"No answer: {result.problems[0]}")

    _log(request_id, 200, started, cost=result.cost_usd)
    return ExtractResponse(
        request_id=request_id,
        fields={f.name: result.answer[f.name] for f in ALL_FIELDS},
        problems=result.problems,
        review=review_of(result),
        model=result.model,
        prompt=result.prompt,
        attempts=len(result.attempts),
        cost_usd=round(result.cost_usd, 5),
        seconds=seconds,
    )


def _log(request_id: str, status: int, started: float, *, cost: float = 0.0, error: str | None = None) -> None:
    """One JSON line per request, for the host's log search. Never the document or its values: a return
    is public, but an uploaded draft might not be."""
    line = {
        "event": "extract",
        "request_id": request_id,
        "status": status,
        "seconds": round(time.monotonic() - started, 2),
    }
    line |= {"cost_usd": round(cost, 5), "model": MODEL} | ({"error": error} if error else {})
    logger.info(json.dumps(line))
