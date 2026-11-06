"""Traces with OpenTelemetry: one trace per request, with a span for each step on its path (design section 8, step 7
stage 3).

Plain language: a trace is a stopwatch breakdown of one request. The request is the outer span; inside it, each
step (checking the key, the limits, the guardrails, every call to a provider, writing the ledger) is a child span
with its own start, end and outcome. A slow request shows where its time went; a failed one shows which step failed.

Spans are sent over OTLP (OpenTelemetry's protocol) to whatever OTEL_EXPORTER_OTLP_ENDPOINT names: locally Jaeger
(docker-compose.yml, profile `observability`), in a cluster a collector. With no endpoint set, OpenTelemetry's
default no-op tracer is used and the spans cost next to nothing. The standard OTEL_* variables apply
(OTEL_SERVICE_NAME, OTEL_TRACES_SAMPLER, ...).

What a span may carry: names, ids, routes, teams, models, outcomes, token counts and cost. Never the messages or the
answer (decision 15). Attribute names follow OpenTelemetry's conventions where one exists (`http.*`, and `gen_ai.*`
for model calls); the rest are `gateway.*`.

The request's own span comes from FastAPI, which (since 0.16x) opens an OpenTelemetry server span per request by
itself, following the HTTP conventions: it reads a caller's W3C `traceparent` header (so the gateway's spans join the
caller's trace), and ends when the last byte is sent, a stream's included. The gateway doesn't open a second one; it
adds what only it knows (team, route, outcome, model, tokens, cost) to FastAPI's, and opens the step spans inside it.
The request id (`x-request-id`) is on that span and the log line has the trace id, so either finds the other.
"""

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

tracer = trace.get_tracer("gateway")  # a proxy: it uses whatever provider is set, now or later


def configure() -> bool:
    """Export spans when an OTLP endpoint is configured; once per process. Returns whether spans are exported."""
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return True  # already set up (by an earlier app in this process, or a test)
    if not (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": "ai-gateway"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # sends in batches, off the request path
    trace.set_tracer_provider(provider)
    return True


def flush() -> None:
    """Send the spans still waiting in the batch, so a stopping process doesn't drop its last requests."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush(timeout_millis=5000)


def trace_id(span: trace.Span) -> str | None:
    context = span.get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


# The request's log fields that become attributes of its span (logs.py has the same fields as a JSON line). FastAPI
# sets the HTTP ones (method, path, route, status) and, for a 5xx, `error.type` and the error status itself.
ATTRIBUTES = {
    "request_id": "gateway.request_id", "error": "gateway.error",
    "team": "gateway.team", "route": "gateway.route", "stream": "gateway.stream", "outcome": "gateway.outcome",
    "provider": "gen_ai.provider.name", "model": "gen_ai.response.model", "fallback": "gateway.fallback",
    "input_tokens": "gen_ai.usage.input_tokens", "output_tokens": "gen_ai.usage.output_tokens",
    "cost_usd": "gateway.cost_usd", "screen_cost_usd": "gateway.judge_cost_usd", "guardrails": "gateway.guardrails",
    "first_token_ms": "gateway.first_token_ms", "prompt": "gateway.prompt",
}  # fmt: skip


def annotate(span: trace.Span, fields: dict) -> None:
    """What the gateway knows about the request, on FastAPI's span for it (which FastAPI ends)."""
    for key, name in ATTRIBUTES.items():
        if (value := fields.get(key)) is not None:
            span.set_attribute(name, float(value) if key.endswith("cost_usd") else value)
