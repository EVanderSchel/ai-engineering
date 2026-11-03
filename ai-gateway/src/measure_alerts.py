"""Each alert made to fire for real, then to resolve: the gateway with injected faults, scraped by the local
Prometheus, which evaluates observability/alerts.yml (step 7 stage 5). Free: stub providers only.

    docker compose up -d --wait db; docker compose --profile observability up -d prometheus
    uv run python src/measure_alerts.py                 # about 25 minutes; writes results/alerts.{json,md}
    uv run python src/measure_alerts.py --report-only   # rebuild the report from the JSON

One gateway, in this process, with a route per fault so every alert can build up at the same time:

| Route        | Provider behaviour                                 | Should fire              |
|--------------|----------------------------------------------------|--------------------------|
| steady       | healthy                                            | nothing (background)     |
| flaky        | 70% of calls fail (its breaker can't trip)         | GatewayHighErrorRate     |
| outage       | first candidate always down, a healthy fallback    | GatewayBreakerOpen       |
| slow         | 18 s per answer                                    | GatewayHighLatency       |
| slow-stream  | 6 s to a stream's first word                       | GatewaySlowFirstToken    |
| priced       | a costly model, for a team with a $1 budget        | GatewayBudgetNearlySpent |

The flaky provider's breaker is set not to trip: otherwise it opens after 5 failures and the scenario becomes the
breaker one. GatewayCostSpike compares a team's last hour with the day before it, so it can't fire in a run this
short; its proof is the promtool tests (observability/alerts_test.yml).

When every target alert has fired (or after FAULT_MINUTES), the faults stop and traffic goes on, to see each alert
resolve. Prometheus's alert states are polled every 5 s and kept as a timeline. Uses the local PostgreSQL: teams
"alerts-demo" and "alerts-budget", price rows for the stub models, keys revoked at the end.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from decimal import Decimal

import httpx2
import psycopg
import uvicorn

import admin
import app as gateway
import config
import migrate
import router
from paths import RESULTS_DIR
from providers import ProviderError
from stub_provider import StubProvider

PROMETHEUS = "http://127.0.0.1:9090/api/v1"
PORT, METRICS_PORT = 8791, 9464
FAULT_MINUTES, RECOVERY_MINUTES = 16, 10
EXPECTED = {
    "GatewayHighErrorRate": "flaky", "GatewayBreakerOpen": "outage", "GatewayHighLatency": "slow",
    "GatewaySlowFirstToken": "slow-stream", "GatewayBudgetNearlySpent": "priced",
}  # fmt: skip
# Each route's traffic: (requests in flight at once, pause between a worker's requests, stream).
TRAFFIC = {
    "steady": (1, 0.8, False), "flaky": (1, 0.8, False), "outage": (1, 1.5, False),
    "slow": (3, 0.5, False), "slow-stream": (2, 1.0, True), "priced": (1, 3.0, False),
}  # fmt: skip
PRICED_USD_PER_MILLION = 1000  # about $0.02 a request: a $1 budget reaches 90% in a few minutes
OUT = RESULTS_DIR / "alerts.json"


def candidate(provider: str, model: str = "stub-echo") -> config.Candidate:
    return config.Candidate(provider=provider, model=model)


def scenario_routes() -> dict[str, config.Route]:
    def route(*candidates, **timeouts) -> config.Route:
        return config.Route(candidates=list(candidates), max_tokens_ceiling=100, timeout=config.Timeouts(**timeouts))

    return {
        "steady": route(candidate("stub")),
        "flaky": route(candidate("flaky")),
        "outage": route(candidate("down"), candidate("stub")),
        "slow": route(candidate("slow"), attempt=60, total=90),
        "slow-stream": route(candidate("slowstream"), first_token=20),
        "priced": route(candidate("priced", "stub-priced")),
    }


def scenario_providers() -> dict[str, StubProvider]:
    unavailable = ProviderError("unavailable", "", "injected outage")
    return {
        "stub": StubProvider("stub"),
        "flaky": StubProvider("flaky", fail=unavailable, error_rate=0.7, seed=1),
        "down": StubProvider("down", fail=unavailable),
        "slow": StubProvider("slow", first_token_delay=18),
        "slowstream": StubProvider("slowstream", first_token_delay=6),
        "priced": StubProvider("priced"),
    }


class ScenarioBreakers(router.Breakers):
    """The usual breakers, except the flaky provider's, which never trips (see the module docstring)."""

    def __getitem__(self, cand: config.Candidate) -> router.Breaker:
        key = (cand.provider, cand.model)
        if key not in self.by_candidate:
            self.by_candidate[key] = router.Breaker(threshold=10**9) if cand.provider == "flaky" else router.Breaker()
        return self.by_candidate[key]


def stop_faults(providers: dict[str, StubProvider]) -> None:
    providers["flaky"].fail = providers["down"].fail = None
    providers["slow"].first_token_delay = providers["slowstream"].first_token_delay = 0


# --- the database: teams, keys and prices for the run ---


def prepare_database(url: str) -> dict[str, str]:
    """Keys for the two teams (rate limits raised: the traffic is well over 60 a minute), price rows for the stubs."""
    models = [("flaky", "stub-echo", 0), ("down", "stub-echo", 0), ("slow", "stub-echo", 0),
              ("slowstream", "stub-echo", 0), ("priced", "stub-priced", PRICED_USD_PER_MILLION)]  # fmt: skip
    with psycopg.connect(url, autocommit=True) as conn:
        for provider, model, price in models:
            conn.execute(
                "insert into prices values (%s, %s, %s, %s, 0, %s, '2026-01-01') on conflict do nothing",
                (provider, model, price, price, price),
            )
        keys = {}
        for team, routes in (("alerts-demo", [r for r in TRAFFIC if r != "priced"]), ("alerts-budget", ["priced"])):
            if not conn.execute("select 1 from teams where name = %s", (team,)).fetchone():
                admin.add_team(conn, team, routes)
            keys[team] = admin.issue_key(conn, team)
            conn.execute("update api_keys set requests_per_minute = 100000 where prefix = %s", (keys[team][:10],))
        # A fresh $1 budget for this run: last run's spend this month is set aside.
        conn.execute("delete from budgets where team_id = (select id from teams where name = 'alerts-budget')")
        admin.set_budget(conn, "alerts-budget", Decimal("1.00"))
    return keys


def revoke(url: str, keys: dict[str, str]) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        for key in keys.values():
            admin.revoke_key(conn, key[:10])


# --- the run ---


@contextlib.asynccontextmanager
async def served(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        server.should_exit = True
        await task


async def worker(client: httpx2.AsyncClient, route: str, key: str, pause: float, stream: bool, counts: dict):
    body = {
        "model": route,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "a request from the alerts run"}],
    }
    headers = {"authorization": f"Bearer {key}"}
    while True:
        try:
            if stream:
                async with client.stream("POST", "/v1/chat/completions", json=body | {"stream": True},
                                         headers=headers) as response:  # fmt: skip
                    await response.aread()
            else:
                response = await client.post("/v1/chat/completions", json=body, headers=headers)
            counts[(route, response.status_code)] = counts.get((route, response.status_code), 0) + 1
        except httpx2.HTTPError as error:
            counts[(route, type(error).__name__)] = counts.get((route, type(error).__name__), 0) + 1
        await asyncio.sleep(pause)


async def alert_states(client: httpx2.AsyncClient) -> dict[str, str]:
    """{alertname: "pending" | "firing"} right now (an alert with several label sets: its most advanced state)."""
    alerts = (await client.get(f"{PROMETHEUS}/alerts")).json()["data"]["alerts"]
    states: dict[str, str] = {}
    for alert in alerts:
        name = alert["labels"]["alertname"]
        if states.get(name) != "firing":
            states[name] = alert["state"]
    return states


def timeline(polls: list[tuple[float, dict[str, str]]], faults_stopped: float) -> dict[str, dict]:
    """From (seconds since faults started, states) polls: when each alert was first pending, first firing, and
    resolved: the first poll it was gone for good, if it was still active when the faults stopped (one that went
    quiet by itself earlier wasn't resolved by stopping them). Times in seconds since the faults started."""
    out: dict[str, dict] = {}
    last_seen: dict[str, float] = {}
    for t, states in polls:
        for name, state in states.items():
            entry = out.setdefault(name, {"pending": None, "firing": None, "resolved": None})
            if entry["pending"] is None:
                entry["pending"] = t
            if state == "firing" and entry["firing"] is None:
                entry["firing"] = t
            last_seen[name] = t
    last_poll_before_stop = max((t for t, _ in polls if t <= faults_stopped), default=0)
    for name, entry in out.items():
        later = [t for t, _ in polls if t > last_seen[name]]
        if later and last_seen[name] >= last_poll_before_stop:
            entry["resolved"] = later[0]
    return out


async def run(url: str) -> dict:
    keys = prepare_database(url)
    os.environ["DATABASE_URL"] = url
    os.environ.setdefault("GATEWAY_LOG_LEVEL", "WARNING")  # 25 minutes of request lines would bury the progress
    providers = scenario_providers()
    app = gateway.create_app(scenario_routes(), providers, breakers=ScenarioBreakers(), metrics_port=METRICS_PORT)
    counts: dict = {}
    polls: list[tuple[float, dict[str, str]]] = []
    try:
        async with served(app), httpx2.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=120) as client:
            tasks = [
                asyncio.create_task(worker(client, route, keys["alerts-budget" if route == "priced" else "alerts-demo"],
                                           pause, stream, counts))
                for route, (workers, pause, stream) in TRAFFIC.items() for _ in range(workers)
            ]  # fmt: skip
            started = time.monotonic()
            stopped = None
            while True:
                await asyncio.sleep(5)
                now = time.monotonic() - started
                states = await alert_states(client)
                polls.append((round(now), states))
                print(f"{now / 60:5.1f} min  {states or '(no alerts)'}", flush=True)
                if stopped is None:
                    all_fired = all(states.get(name) == "firing" for name in EXPECTED)
                    if all_fired or now > FAULT_MINUTES * 60:
                        stop_faults(providers)
                        stopped = now
                        print(f"faults stopped at {now / 60:.1f} min", flush=True)
                elif now - stopped > RECOVERY_MINUTES * 60 or not set(states) - {"GatewayBudgetNearlySpent"}:
                    break  # all resolved (the budget stays spent, so its alert stays: that's correct)
            for task in tasks:
                task.cancel()
    finally:
        revoke(url, keys)
    return {
        "faults_stopped_s": round(stopped),
        "expected": EXPECTED,
        "timeline": timeline(polls, stopped),
        "polls": polls,
        "responses": {f"{route} {status}": n for (route, status), n in sorted(counts.items(), key=str)},
    }


def report(result: dict) -> str:
    rows = result["timeline"]
    minutes = lambda s: "-" if s is None else f"{s / 60:.1f}"  # noqa: E731
    lines = [
        "# Alerts fired for real",
        "",
        "Generated by `src/measure_alerts.py`: the gateway with injected faults (one route per fault), scraped every "
        "5 s by the local Prometheus, evaluating `observability/alerts.yml`. Minutes from the start of the faults; "
        f"the faults stopped at {minutes(result['faults_stopped_s'])} min.",
        "",
        "| Alert | Route with the fault | Pending | Firing | Resolved |",
        "|---|---|---|---|---|",
    ]
    for name, route in result["expected"].items():
        entry = rows.get(name, {})
        lines.append(f"| {name} | {route} | {minutes(entry.get('pending'))} | {minutes(entry.get('firing'))} "
                     f"| {minutes(entry.get('resolved'))} |")  # fmt: skip
    others = sorted(set(rows) - set(result["expected"]))
    lines += [
        "",
        f"Other alerts seen: {', '.join(others) if others else 'none'}. GatewayCostSpike can't fire in a run this "
        "short (it compares the last hour with the day before); its proof is the promtool tests.",
        "",
        "Responses by route and status:",
        "",
    ]
    lines += [f"- {k}: {v}" for k, v in result["responses"].items()]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.report_only:
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        result = asyncio.run(run(migrate.database_url()), loop_factory=loop_factory)
        OUT.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    result = json.loads(OUT.read_text(encoding="utf-8"))
    (RESULTS_DIR / "alerts.md").write_text(report(result), encoding="utf-8")
    print(report(result))


if __name__ == "__main__":
    main()
