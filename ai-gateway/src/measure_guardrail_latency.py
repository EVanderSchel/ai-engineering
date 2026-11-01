"""What the guardrails cost in time, measured with the gateway in memory and the stub provider. Free.

    uv run python src/measure_guardrail_latency.py      # writes results/guardrail-latency.{json,md}

Three questions:

1. **Overhead per policy**: the same 2 KB request, 200 times one after another, with no guardrails, the Claude
   routes' defaults (flag personal data, pattern screen), and a strict policy without the judge (redact in and out,
   block injections). The stub answers instantly, so the difference is the guardrails' own cost (plus noise).
2. **Streams** (behind a real uvicorn server on localhost): time to the first word when the answer streams as it
   comes, against a route that must check the
   whole answer first (decision 14). The stub is paced like a model: first word after 300 ms, then 40 words at
   20 ms each.
3. **The judge**: its latency and cost come from its 60 real calls in results/injection-judge-*.json (no new calls).
"""

import asyncio
import contextlib
import json
import statistics
import sys
import time

import httpx2
import uvicorn

import app as gateway
import config
import keys
from paths import RESULTS_DIR
from stub_provider import StubProvider

KEY = "gw_latency-measurement-key"
RUNS = 200
PROMPT = (
    "Summarise the following meeting notes for the board in five bullet points. "
    + "The committee reviewed fourteen applications and discussed the timeline for the spring round. " * 20
    + "Contact the coordinator at dana.ortiz@example.com with questions."
)  # about 2 KB, with one email address in it

POLICIES = {
    "none": config.Guardrails(),
    "defaults (flag, patterns)": config.Guardrails(pii_input="flag", injection="flag"),
    "strict, no judge": config.Guardrails(pii_input="redact", pii_output="redact", injection="block"),
}


def make_app(policy: config.Guardrails, stub: StubProvider):
    route = config.Route(
        candidates=[config.Candidate(provider="stub", model="stub-echo")], max_tokens_ceiling=200, guardrails=policy
    )
    return gateway.create_app({"r": route}, {"stub": stub}, {keys.key_hash(KEY): "measure"})


def make_client(policy: config.Guardrails, stub: StubProvider) -> httpx2.AsyncClient:
    """In memory: right for whole responses. Not for streams: the ASGI transport hands back a response only once
    the app has finished it, so every word would seem to arrive at the end."""
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=make_app(policy, stub)), base_url="http://gw",
        headers={"authorization": f"Bearer {KEY}"},
    )  # fmt: skip


@contextlib.asynccontextmanager
async def served(app, port: int = 8799):
    """The app behind a real uvicorn server on localhost, so streamed words arrive when they're sent."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        async with httpx2.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers={"authorization": f"Bearer {KEY}"}, timeout=30
        ) as client:
            yield client
    finally:
        server.should_exit = True
        await task


def stats_ms(seconds: list[float]) -> dict:
    ordered = sorted(seconds)
    return {
        "p50_ms": round(1000 * statistics.median(ordered), 2),
        "p95_ms": round(1000 * ordered[round(0.95 * (len(ordered) - 1))], 2),
    }


async def overhead() -> dict:
    body = {"model": "r", "max_tokens": 50, "messages": [{"role": "user", "content": PROMPT}]}
    out = {}
    for name, policy in POLICIES.items():
        async with make_client(policy, StubProvider()) as client:
            for _ in range(20):  # warm up
                await client.post("/v1/chat/completions", json=body)
            times = []
            for _ in range(RUNS):
                started = time.perf_counter()
                response = await client.post("/v1/chat/completions", json=body)
                times.append(time.perf_counter() - started)
                assert response.status_code == 200, response.text
        out[name] = stats_ms(times)
    base = out["none"]["p50_ms"]
    for row in out.values():
        row["added_p50_ms"] = round(row["p50_ms"] - base, 2)
    return out


async def streams(runs: int = 20) -> dict:
    words = " ".join(f"word{i}" for i in range(36))  # with the stub's "Stub reply to:" prefix: 40 words
    body = {"model": "r", "max_tokens": 100, "stream": True, "messages": [{"role": "user", "content": words}]}
    out = {}
    policies = {
        "streamed as it comes": config.Guardrails(pii_output="flag"),
        "checked first (pii_output=redact)": config.Guardrails(pii_output="redact"),
    }
    for name, policy in policies.items():
        first_word, total = [], []
        async with served(make_app(policy, StubProvider(first_token_delay=0.3, delay=0.02))) as client:
            for _ in range(runs):
                started = time.perf_counter()
                first = None
                async with client.stream("POST", "/v1/chat/completions", json=body) as response:
                    async for line in response.aiter_lines():
                        if first is None and '"content":"' in line and '"content":""' not in line:
                            first = time.perf_counter() - started
                first_word.append(first)
                total.append(time.perf_counter() - started)
        out[name] = {"first_word": stats_ms(first_word), "total": stats_ms(total)}
    return out


def judge_from_runs() -> dict:
    seconds, costs = [], []
    for split in ("dev", "held_out"):
        path = RESULTS_DIR / f"injection-judge-{split}.json"
        if path.exists():
            answers = json.loads(path.read_text(encoding="utf-8"))["answers"]
            seconds += [a["seconds"] for a in answers]
            costs += [float(a["cost_usd"]) for a in answers]
    if not seconds:
        return {}
    return {"calls": len(seconds), **stats_ms(seconds), "cost_per_call_usd": round(statistics.mean(costs), 6)}


def report(result: dict) -> str:
    lines = [
        "# What the guardrails cost in time",
        "",
        "Generated by `src/measure_guardrail_latency.py`: the gateway with the stub provider (in memory; streams "
        "behind a local uvicorn server, since the in-memory transport only returns a finished response), so the "
        "numbers are the gateway's own work on this PC (an i7-6700), not a provider's.",
        "",
        f"## Overhead per policy ({RUNS} requests of about 2 KB, one at a time)",
        "",
        "| Policy | p50 | p95 | Added at p50 |",
        "|---|---|---|---|",
    ]
    for name, row in result["overhead"].items():
        lines.append(f"| {name} | {row['p50_ms']} ms | {row['p95_ms']} ms | {row['added_p50_ms']:+} ms |")
    lines += [
        "",
        "## Streams: first word vs. whole answer (stub paced like a model: 300 ms, then 40 words at 20 ms)",
        "",
        "| Route's output policy | First word, p50 | Whole answer, p50 |",
        "|---|---|---|",
    ]
    for name, row in result["streams"].items():
        lines.append(f"| {name} | {row['first_word']['p50_ms']} ms | {row['total']['p50_ms']} ms |")
    if judge := result["judge"]:
        lines += [
            "",
            f"## The injection judge ({judge['calls']} real calls, from results/injection-judge-*.json)",
            "",
            f"p50 {judge['p50_ms']} ms, p95 {judge['p95_ms']} ms, about ${judge['cost_per_call_usd']} per call "
            "(Claude Haiku 5.5, low effort).",
        ]
    return "\n".join(lines) + "\n"


async def main() -> dict:
    return {"overhead": await overhead(), "streams": await streams(), "judge": judge_from_runs()}


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    result = asyncio.run(main(), loop_factory=loop_factory)
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "guardrail-latency.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (RESULTS_DIR / "guardrail-latency.md").write_text(report(result), encoding="utf-8")
    print(report(result))
