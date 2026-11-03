"""What logs, metrics and traces cost per request (step 7 stage 5). Free: in memory, the stub provider, no database.

    uv run python src/measure_observability.py      # writes results/observability.{json,md}

1. **Per request, end to end**: the same small request, 2,000 times one after another, through the gateway in memory
   (httpx2's ASGI transport), in three set-ups:
   - "metrics only": the log level at WARNING (the request line is skipped before it's formatted), tracing off
     (OpenTelemetry's no-op default). Metrics can't be turned off, so they're in every set-up and timed in part 2;
   - "+ request log": the JSON line written for every request (to a discarded stream, as stdout would be);
   - "+ traces": OpenTelemetry's SDK recording every span (FastAPI's and the gateway's) and passing them to a
     batching exporter that discards them, so the cost is making the spans, not the network.
   Rounds alternate between set-ups to spread out drift; the traced rounds come last because OpenTelemetry allows its
   provider to be set only once per process. Medians of the per-request times.
2. **Per call**: each piece alone, timed over 20,000 calls with a typical request's fields: counting a request in
   the metrics, formatting its log line, and annotating a span.

The stub answers in microseconds and nothing touches a database, so the numbers are the instrumentation's own cost,
plus the gateway's in-memory path for comparison. Against the 30-45 ms that PostgreSQL adds per request on this PC
(stage 3), and the seconds a real model takes, that's the scale to read them at.
"""

import asyncio
import io
import json
import logging
import statistics
import sys
import time
import timeit

import httpx2
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

import app as gateway
import config
import keys
import logs
import metrics
import router
import tracing
from paths import RESULTS_DIR
from stub_provider import StubProvider

KEY = "gw_observability-measurement-key"
REQUESTS, ROUNDS, CALLS = 2000, 3, 20000
BODY = {"model": "stub", "max_tokens": 50, "messages": [{"role": "user", "content": "how do bees make honey?"}]}
FIELDS = {
    "request_id": "0" * 32, "method": "POST", "path": "/v1/chat/completions", "status": 200, "duration_ms": 412,
    "team": "research", "route": "stub", "stream": False, "first_token_ms": None,
    "attempts": [{"provider": "stub", "model": "stub-echo", "outcome": "ok", "ms": 0}], "fallback": False,
    "outcome": "ok", "provider": "stub", "model": "stub-echo", "input_tokens": 12, "output_tokens": 40,
    "cost_usd": 0.0004, "guardrails": "pii_input=flagged:email", "trace_id": "1" * 32,
}  # fmt: skip


class Discard(SpanExporter):
    def export(self, spans):
        return SpanExportResult.SUCCESS


def routes() -> dict[str, config.Route]:
    return {"stub": config.Route(candidates=[config.Candidate(provider="stub", model="stub-echo")],
                                 max_tokens_ceiling=100)}  # fmt: skip


async def per_request_times(level: int) -> list[float]:
    logging.getLogger("gateway").setLevel(level)
    app = gateway.create_app(routes(), {"stub": StubProvider()}, {keys.key_hash(KEY): "measure"})
    transport = httpx2.ASGITransport(app=app)
    times = []
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw",
                                  headers={"authorization": f"Bearer {KEY}"}) as client:  # fmt: skip
        for _ in range(50):  # warm up
            await client.post("/v1/chat/completions", json=BODY)
        for _ in range(REQUESTS):
            started = time.perf_counter()
            response = await client.post("/v1/chat/completions", json=BODY)
            times.append(time.perf_counter() - started)
            assert response.status_code == 200
    return times


def summary(times: list[float]) -> dict:
    ordered = sorted(times)
    return {
        "requests": len(times),
        "p50_us": round(statistics.median(times) * 1e6),
        "p95_us": round(ordered[int(0.95 * (len(ordered) - 1))] * 1e6),
        "mean_us": round(statistics.mean(times) * 1e6),
    }


async def end_to_end() -> dict:
    results: dict[str, list[float]] = {"metrics only": [], "+ request log": [], "+ traces": []}
    for _ in range(ROUNDS):
        results["metrics only"] += await per_request_times(logging.WARNING)
        results["+ request log"] += await per_request_times(logging.INFO)
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(Discard()))
    trace.set_tracer_provider(provider)
    for _ in range(ROUNDS):
        results["+ traces"] += await per_request_times(logging.INFO)
    provider.shutdown()
    return {name: summary(times) for name, times in results.items()}


def per_call() -> dict:
    gauges = metrics.Metrics(routes(), router.Breakers())
    formatter = logs.JsonFormatter()
    record = logging.LogRecord("gateway", logging.INFO, __file__, 1, "request", None, None)
    record.fields = FIELDS
    span = trace.get_tracer("measure").start_span("measure")  # a recording span: the SDK provider is set by now
    timings = {
        "count in the metrics (metrics.observe)": lambda: gauges.observe(FIELDS),
        "format the log line (JsonFormatter)": lambda: formatter.format(record),
        "annotate the request span (tracing.annotate)": lambda: tracing.annotate(span, FIELDS),
    }
    return {name: round(timeit.timeit(fn, number=CALLS) / CALLS * 1e6, 2) for name, fn in timings.items()}


def report(result: dict) -> str:
    e2e, calls = result["end_to_end"], result["per_call_us"]
    base = e2e["metrics only"]["p50_us"]
    lines = [
        "# What observability costs per request",
        "",
        "Generated by `src/measure_observability.py` on this PC (Intel i7-6700, Windows, Python 3.14): the gateway in "
        f"memory, the stub provider, no database; {e2e['metrics only']['requests']:,} requests per set-up "
        f"({ROUNDS} alternating rounds), medians.",
        "",
        "| Set-up | p50 | p95 | Added to p50 |",
        "|---|---|---|---|",
    ]
    for name, s in e2e.items():
        added = "(baseline)" if name == "metrics only" else f"+{s['p50_us'] - base} µs"
        lines.append(f"| {name} | {s['p50_us']:,} µs | {s['p95_us']:,} µs | {added} |")
    lines += ["", "Each piece alone, per call (with a typical request's fields):", ""]
    lines += [f"- {name}: {us} µs" for name, us in calls.items()]
    lines += [
        "",
        "For scale: the stub's whole path through the gateway in memory is the baseline above; PostgreSQL adds "
        "30-45 ms per request on this PC (step 7 stage 3), and a real model 0.3 s to a minute.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    real_stdout = sys.stdout
    sys.stdout = io.StringIO()  # the request lines go here, discarded, as a container's stdout would take them
    try:
        e2e = asyncio.run(end_to_end())
        calls = per_call()
    finally:
        sys.stdout = real_stdout
    result = {"end_to_end": e2e, "per_call_us": calls}
    (RESULTS_DIR / "observability.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (RESULTS_DIR / "observability.md").write_text(report(result), encoding="utf-8")
    print(report(result))


if __name__ == "__main__":
    main()
