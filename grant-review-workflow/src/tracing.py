"""Spans for the workflow's steps (step 7, stage 5), with OpenTelemetry.

The code depends only on opentelemetry-api, which records nothing by itself: locally and in tests every span is a
no-op. In Lambda, AWS Distro for OpenTelemetry (unpacked into the image, see Dockerfile) supplies the SDK, records
the spans, and sends them to X-Ray with the AWS SDK calls made inside each step (Bedrock, DynamoDB, S3) nested
under them. At runtime the layer's own opentelemetry-api comes first on the path; only the stable API is used here.

Spans carry identifiers and numbers, never applicant text (docs/data-handling.md): the application ID, the step,
model calls, cost. A failure is recorded by its exception type alone, since messages (Pydantic's in particular)
can quote input. The reviewer pause is LangGraph raising a GraphInterrupt: that's marked "paused", not an error.
"""

import functools

from langgraph.errors import GraphBubbleUp
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("grant-review-workflow")


def traced(step: str, node):
    """A graph node wrapped in a span named after its step."""

    @functools.wraps(node)
    def run(state):
        with tracer.start_as_current_span(
            f"workflow.{step}", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("grant_review.step", step)
            span.set_attribute("grant_review.application_id", state.application_id)
            try:
                update = node(state)
            except GraphBubbleUp:
                span.set_attribute("grant_review.paused", True)  # waiting for the reviewer: not a failure
                raise
            except Exception as e:
                span.set_attribute("error.type", type(e).__name__)
                span.set_status(Status(StatusCode.ERROR, type(e).__name__))
                raise
            calls = (update or {}).get("calls", [])
            if calls:
                span.set_attribute("grant_review.model_calls", len(calls))
                span.set_attribute("grant_review.cost_usd", round(sum(c.cost_usd for c in calls), 6))
                span.set_attribute("grant_review.models", sorted({c.model for c in calls}))
            return update

    return run


def current_trace_id() -> str | None:
    """The active trace's ID in X-Ray's format (1-<8 hex>-<24 hex>), for log lines; None when not tracing."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    hex_id = format(context.trace_id, "032x")
    return f"1-{hex_id[:8]}-{hex_id[8:]}"
