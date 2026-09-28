"""Langfuse tracing: one trace per request, with a retrieval step and a generation step inside it.

Tracing is optional. Without LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY (or with
LANGFUSE_TRACING_ENABLED=false, which the tests set) every function here is a no-op, so tests,
CI, and fresh clones behave exactly as before.

Spans are started and ended explicitly rather than with `with` blocks: a streaming response is
produced by a generator that Starlette steps through from worker threads, and OpenTelemetry's
"current span" tracking doesn't survive being handed between threads mid-block.
"""

import os
import pathlib

from dotenv import load_dotenv

# Local runs read keys from rag/.env; in Docker, compose injects them as environment variables.
# load_dotenv never overrides a variable that is already set.
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

_client = None
if (
    os.environ.get("LANGFUSE_PUBLIC_KEY")
    and os.environ.get("LANGFUSE_SECRET_KEY")
    and os.environ.get("LANGFUSE_TRACING_ENABLED", "true").lower() != "false"
):
    from langfuse import Langfuse

    _client = Langfuse()


class _NoOp:
    """Stands in for a Langfuse observation when tracing is off; every method does nothing."""

    def start_observation(self, **kwargs):
        return self

    def update(self, **kwargs):
        return self

    def set_trace_io(self, **kwargs):
        return self

    def end(self, **kwargs):
        return self


def enabled() -> bool:
    return _client is not None


def start_trace(request_id: str, name: str, input, metadata: dict):
    """Open the root observation of a new trace. Its trace ID is the request ID from our logs,
    so a log line can be looked up in Langfuse directly."""
    if _client is None:
        return _NoOp()
    root = _client.start_observation(
        trace_context={"trace_id": request_id}, name=name, input=input, metadata=metadata
    )
    root.set_trace_io(input=input)
    return root


def flush() -> None:
    """Send any buffered traces. Langfuse batches in the background, so call this on shutdown."""
    if _client is not None:
        _client.flush()
