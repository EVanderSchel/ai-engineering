"""Measure the router under injected outages: how many requests still get an answer, and what it costs in time.

    uv run python src/measure_resilience.py            # writes results/resilience.{json,md}

Every scenario runs the real gateway app in memory (an ASGI transport: no network, no providers, no cost) with two
fault-injecting stub candidates, "primary" and "backup", and sends the same batch of concurrent requests. Each one
is run three ways (MODES): the gateway as configured; the gateway with only the primary (retries and breaker but
nothing to fall back to); and "direct", one try at the primary with no breaker, which is what an app calling the
provider itself would get.

Times are scaled down (a stub answers in tens of milliseconds, timeouts are fractions of a second, a breaker cools
down in 1 s instead of 30) so the whole run takes under a minute; the shape of the results, not the absolute
milliseconds, is what carries over.
"""

import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field

import httpx2

import app as gateway
import config
import keys
import router
from paths import RESULTS_DIR
from providers import ProviderError
from stub_provider import StubProvider

KEY = "gw_measurement-only-key"
REQUESTS = 200
CONCURRENCY = 10
FIRST_TOKEN = 0.02  # a healthy stub's time to its first word (seconds)
PER_WORD = 0.002
TIMEOUTS = config.Timeouts(first_token=0.2, attempt=0.4, total=1.0)
BREAKER = {"threshold": 5, "window": 3.0, "cooldown": 1.0}
DOWN = ProviderError("unavailable", "injected outage", "?")


@dataclass
class Scenario:
    name: str
    description: str
    primary: dict = field(default_factory=dict)  # StubProvider faults
    backup: dict = field(default_factory=dict)
    stream: bool = False


SCENARIOS = [
    Scenario("healthy", "Both candidates healthy: the cost of the gateway's checks when nothing is wrong"),
    Scenario("primary down", "The primary fails every request (a hard outage)", primary={"fail": DOWN}),
    Scenario("primary flaky", "The primary fails 30% of requests at random", primary={"fail": DOWN, "error_rate": 0.3}),
    Scenario("primary slow", "The primary takes 2 s to start answering", primary={"first_token_delay": 2.0}),
    Scenario("both down", "Both candidates fail every request", primary={"fail": DOWN}, backup={"fail": DOWN}),
    Scenario(
        "primary down, streamed", "Hard outage of the primary, streamed requests", primary={"fail": DOWN}, stream=True
    ),
    Scenario(
        "primary drops streams",
        "The primary fails 30% of streams after 3 words: too late to switch (decision 10)",
        primary={"fail": DOWN, "error_rate": 0.3, "fail_after_words": 3},
        stream=True,
    ),
]


def stub(name: str, faults: dict) -> StubProvider:
    return StubProvider(name, **{"first_token_delay": FIRST_TOKEN, "delay": PER_WORD, "seed": 7, **faults})


MODES = {
    # name: (candidates, retries, breaker)
    "gateway": (2, 1, True),  # the gateway as configured: retries, fallback to the backup, breakers
    "one candidate": (1, 1, True),  # the gateway's retries and breaker, but nothing to fall back to
    "direct": (1, 0, False),  # an app calling the primary itself: one try, no breaker
}


def make_app(scenario: Scenario, mode: str = "gateway"):
    n_candidates, retries, breaker = MODES[mode]
    candidates = [config.Candidate(provider="primary", model="primary-model")]
    if n_candidates == 2:
        candidates.append(config.Candidate(provider="backup", model="backup-model"))
    route = config.Route(candidates=candidates, max_tokens_ceiling=100, timeout=TIMEOUTS)
    providers = {"primary": stub("primary", scenario.primary), "backup": stub("backup", scenario.backup)}
    settings = BREAKER if breaker else {**BREAKER, "threshold": 10**9}  # a breaker that never opens
    breakers = router.Breakers(lambda: router.Breaker(**settings))
    policy = router.Policy(retries=retries, backoff=0.02, max_wait=0.1)
    return gateway.create_app({"r": route}, providers, {keys.key_hash(KEY): "measure"}, breakers, policy)


async def one_request(client: httpx2.AsyncClient, i: int, stream: bool) -> dict:
    body = {"model": "r", "max_tokens": 20, "stream": stream, "messages": [{"role": "user", "content": f"request {i}"}]}
    started = time.perf_counter()
    if not stream:
        response = await client.post("/v1/chat/completions", json=body)
        ok = response.status_code == 200
        return {
            "ok": ok,
            "status": response.status_code,
            "seconds": time.perf_counter() - started,
            "provider": response.headers.get("x-gateway-provider"),
            "attempts": int(response.headers.get("x-gateway-attempts", 0)),
            "code": None if ok else response.json()["error"]["code"],
        }
    async with client.stream("POST", "/v1/chat/completions", json=body) as response:
        text = (await response.aread()).decode()
    # A stream is only a success if it ended properly; an error event after the first byte is a failure.
    ok = response.status_code == 200 and text.rstrip().endswith("data: [DONE]")
    code = None
    if not ok:
        code = "error_mid_stream" if response.status_code == 200 else json.loads(text)["error"]["code"]
    return {
        "ok": ok,
        "status": response.status_code,
        "seconds": time.perf_counter() - started,
        "provider": response.headers.get("x-gateway-provider"),
        "attempts": int(response.headers.get("x-gateway-attempts", 0)),
        "code": code,
    }


async def run(scenario: Scenario, mode: str) -> list[dict]:
    app = make_app(scenario, mode)
    transport = httpx2.ASGITransport(app=app)
    limit = asyncio.Semaphore(CONCURRENCY)
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://gateway", headers={"authorization": f"Bearer {KEY}"}, timeout=30
    ) as client:

        async def limited(i: int) -> dict:
            async with limit:
                return await one_request(client, i, scenario.stream)

        return await asyncio.gather(*(limited(i) for i in range(REQUESTS)))


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if r["ok"]]
    seconds = [r["seconds"] for r in ok]
    codes: dict[str, int] = {}
    for r in results:
        if not r["ok"]:
            codes[r["code"]] = codes.get(r["code"], 0) + 1
    return {
        "requests": len(results),
        "served": len(ok),
        "served_pct": round(100 * len(ok) / len(results), 1),
        "by_backup": sum(r["provider"] == "backup" for r in ok),
        "p50_ms": round(1000 * statistics.median(seconds), 1) if seconds else None,
        "p95_ms": round(1000 * percentile(seconds, 95), 1) if seconds else None,
        "max_ms": round(1000 * max(r["seconds"] for r in results), 1),
        "over_deadline": sum(r["seconds"] > TIMEOUTS.total + 0.1 for r in results),  # 0.1 s of slack for the client
        "failures": codes,
    }


async def recovery_timeline() -> list[dict]:
    """The primary goes down for 2 s and comes back: the breaker opens, skips it, then a trial closes it again."""
    scenario = Scenario("timeline", "")
    app = make_app(scenario)
    primary = app.state.providers["primary"]
    breaker = app.state.breakers[config.Candidate(provider="primary", model="primary-model")]
    transport = httpx2.ASGITransport(app=app)
    timeline = []
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://gateway", headers={"authorization": f"Bearer {KEY}"}, timeout=30
    ) as client:
        start = time.perf_counter()
        i = 0
        while (elapsed := time.perf_counter() - start) < 5.0:
            primary.fail = DOWN if 1.0 <= elapsed < 3.0 else None
            seen = breaker.state  # the state this request meets, before it changes it
            result = await one_request(client, i, stream=False)
            timeline.append({
                "t": round(elapsed, 2), "ok": result["ok"], "provider": result["provider"],
                "attempts": result["attempts"], "ms": round(1000 * result["seconds"], 1), "breaker": seen,
            })  # fmt: skip
            i += 1
            await asyncio.sleep(0.05)
    return timeline


def timeline_phases(timeline: list[dict]) -> list[dict]:
    """Consecutive requests with the same breaker state and provider, collapsed into phases."""
    phases: list[dict] = []
    for point in timeline:
        key = (point["breaker"], point["provider"], point["attempts"])
        if phases and phases[-1]["key"] == key:
            phases[-1]["requests"] += 1
            phases[-1]["ms"].append(point["ms"])
            phases[-1]["ok"] += point["ok"]
            phases[-1]["until"] = point["t"]
        else:
            phases.append({
                "key": key, "from": point["t"], "until": point["t"], "requests": 1, "ok": int(point["ok"]),
                "ms": [point["ms"]],
            })  # fmt: skip
    return [
        {
            "from_s": p["from"],
            "until_s": p["until"],
            "breaker": p["key"][0],
            "answered_by": p["key"][1],
            "attempts": p["key"][2],
            "requests": p["requests"],
            "ok": p["ok"],
            "median_ms": round(statistics.median(p["ms"]), 1),
        }  # fmt: skip
        for p in phases
    ]


def report(summary: dict) -> str:
    lines = [
        "# Resilience under injected outages",
        "",
        f"Generated by `src/measure_resilience.py`. Each scenario: {REQUESTS} requests, {CONCURRENCY} at a time, "
        "against the gateway in memory with fault-injecting stub candidates. **gateway** = retries, fallback to the "
        "backup and circuit breakers; **one candidate** = the gateway's retries and breaker with nothing to fall "
        "back to; **direct** = one try at the primary and no breaker, as an app calling the provider itself would "
        "get.",
        "",
        f"Scaled-down times: a healthy stub starts answering in {int(FIRST_TOKEN * 1000)} ms; timeouts are "
        f"first token {TIMEOUTS.first_token} s, attempt {TIMEOUTS.attempt} s, total {TIMEOUTS.total} s; breakers "
        f"open after {BREAKER['threshold']} failures in {BREAKER['window']:.0f} s and cool down for "
        f"{BREAKER['cooldown']:.0f} s.",
        "",
        "| Scenario | | Served | By backup | p50 | p95 | Slowest | Over deadline | Failures |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, runs in summary["scenarios"].items():
        for mode in MODES:
            s = runs[mode]
            failures = ", ".join(f"{k} {v}" for k, v in s["failures"].items()) or "-"
            p50 = f"{s['p50_ms']} ms" if s["p50_ms"] is not None else "-"
            p95 = f"{s['p95_ms']} ms" if s["p95_ms"] is not None else "-"
            lines.append(
                f"| {name if mode == 'gateway' else ''} | {mode} | {s['served_pct']}% | {s['by_backup']} | {p50} | "
                f"{p95} | {s['max_ms']} ms | {s['over_deadline']} | {failures} |"
            )
    lines += [
        "",
        "## Recovery timeline",
        "",
        "One request at a time for 5 s; the primary is down from 1 s to 3 s. Breaker = the state each request met.",
        "",
        "| From | Until | Breaker | Answered by | Attempts | Requests | OK | Median |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in summary["timeline"]:
        lines.append(
            f"| {p['from_s']} s | {p['until_s']} s | {p['breaker']} | {p['answered_by']} | {p['attempts']} | "
            f"{p['requests']} | {p['ok']} | {p['median_ms']} ms |"
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    summary: dict = {"scenarios": {}}
    for scenario in SCENARIOS:
        summary["scenarios"][scenario.name] = {"description": scenario.description}
        for mode in MODES:
            summary["scenarios"][scenario.name][mode] = summarize(await run(scenario, mode))
        print(f"{scenario.name}: {summary['scenarios'][scenario.name]['gateway']['served_pct']}% served")
    summary["timeline"] = timeline_phases(await recovery_timeline())
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "resilience.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (RESULTS_DIR / "resilience.md").write_text(report(summary), encoding="utf-8")
    print(f"wrote {RESULTS_DIR / 'resilience.md'}")


if __name__ == "__main__":
    asyncio.run(main())
