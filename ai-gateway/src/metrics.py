"""Prometheus metrics: counters, histograms and gauges that Prometheus scrapes every few seconds and alerts on
(design section 8, step 7 stage 2).

Plain language: a log line describes one request; a metric is a running total or a distribution over all of them
("requests that failed so far", "how many took under a second"), cheap enough to keep forever and to compute rates
and percentiles from.

Served on their own port (GATEWAY_METRICS_PORT; serve.py uses 9464, OpenTelemetry's default for Prometheus), not on
the API's, because they name teams and their spend: callers can reach the API, only Prometheus should reach this.
Each request is counted from the same fields as its log line (app.request_fields), so the two can't disagree.

Labels are kept to values the gateway controls (route names from the config, providers, teams from the database,
fixed outcome words), never anything a caller can make up, so the number of series stays bounded: an unknown route
name a caller sent is counted as "unknown".
"""

import asyncio
import contextlib
import datetime as dt
import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, start_http_server
from prometheus_client.core import GaugeMetricFamily

import db

log = logging.getLogger("gateway.metrics")

# Seconds. LLM answers take from tens of milliseconds (the stub, a cached answer) to a minute or two (a long answer).
DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)
FIRST_TOKEN_BUCKETS = (0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15)
BUDGET_REFRESH_SECONDS = 30


class Metrics:
    def __init__(self, routes, breakers):
        self.routes = set(routes)
        self.registry = registry = CollectorRegistry()  # one per app, so tests can make many apps
        self.requests = Counter(
            "gateway_requests", "Requests answered, by route, HTTP status and outcome",
            ["route", "status", "outcome"], registry=registry,
        )  # fmt: skip
        self.duration = Histogram(
            "gateway_request_duration_seconds", "Time until the whole response was sent (a stream's last event)",
            ["route", "stream"], buckets=DURATION_BUCKETS, registry=registry,
        )  # fmt: skip
        self.first_token = Histogram(
            "gateway_first_token_seconds", "Streams: time until the provider's first token reached the gateway",
            ["route", "provider"], buckets=FIRST_TOKEN_BUCKETS, registry=registry,
        )  # fmt: skip
        self.attempts = Counter(
            "gateway_provider_attempts", "Calls to providers, by outcome (ok, an error kind, or skipped by a breaker)",
            ["provider", "model", "outcome"], registry=registry,
        )  # fmt: skip
        self.fallbacks = Counter(
            "gateway_fallbacks", "Requests answered by a candidate other than the route's first",
            ["route", "provider"], registry=registry,
        )  # fmt: skip
        self.tokens = Counter(
            "gateway_tokens", "Tokens used, by team, route and kind (input, output)",
            ["team", "route", "kind"], registry=registry,
        )  # fmt: skip
        self.cost = Counter(
            "gateway_cost_usd", "Spend in USD, by team, route and kind (model, judge)",
            ["team", "route", "kind"], registry=registry,
        )  # fmt: skip
        self.guardrails = Counter(
            "gateway_guardrail_findings", "Guardrail findings, by check and action",
            ["check", "action"], registry=registry,
        )  # fmt: skip
        self.answers = Counter(
            "gateway_answers", "Answers sent, by route, registered prompt (name@version), finish reason and format "
            "(ok, bad, or empty if the prompt declares none): the drift monitor's free checks",
            ["route", "prompt", "finish", "format"], registry=registry,
        )  # fmt: skip
        self.judge_scores = Histogram(
            "gateway_judge_scores", "The drift judge's grades (1-5) of sampled answers: mean = _sum / _count",
            ["route", "prompt"], buckets=(1, 2, 3, 4, 5), registry=registry,
        )  # fmt: skip
        self.judge_runs = Counter(
            "gateway_judge_runs", "Sampled answers sent to the drift judge, by outcome (graded, declined, busy, error)",
            ["route", "outcome"], registry=registry,
        )  # fmt: skip
        self.judge_cost = Counter(
            "gateway_judge_cost_usd", "The drift judge's spend in USD: the gateway's, not charged to teams",
            ["route"], registry=registry,
        )  # fmt: skip
        self.in_flight = Gauge("gateway_requests_in_flight", "Requests being handled now", registry=registry)
        self.budgets: list[tuple[str, float, float | None]] = []  # (team, spent, limit), refreshed from the database
        registry.register(_Breakers(breakers))
        registry.register(_Budgets(self))

    def route_label(self, route: str | None) -> str:
        if route is None:
            return ""  # not a chat request (the models list)
        return route if route in self.routes else "unknown"

    def observe(self, fields: dict) -> None:
        """Counts one finished request from its log line's fields."""
        route = self.route_label(fields.get("route"))
        status = fields["status"]
        outcome = fields.get("outcome") or ("refused" if status < 500 else "error")  # never admitted
        self.requests.labels(route, str(status), outcome).inc()
        self.duration.labels(route, str(bool(fields.get("stream"))).lower()).observe(fields["duration_ms"] / 1000)
        provider = fields.get("provider")
        if fields.get("first_token_ms") is not None and provider:
            self.first_token.labels(route, provider).observe(fields["first_token_ms"] / 1000)
        for attempt in fields.get("attempts") or []:
            self.attempts.labels(attempt["provider"], attempt["model"], attempt["outcome"]).inc()
        if fields.get("fallback") and provider:
            self.fallbacks.labels(route, provider).inc()
        if team := fields.get("team"):
            for kind in ("input", "output"):
                if tokens := fields.get(f"{kind}_tokens"):
                    self.tokens.labels(team, route, kind).inc(tokens)
            for kind, key in (("model", "cost_usd"), ("judge", "screen_cost_usd")):
                if cost := fields.get(key):
                    self.cost.labels(team, route, kind).inc(float(cost))
        if finish := fields.get("finish"):  # prompt refs come from the registry, finish reasons from translate.py
            self.answers.labels(route, fields.get("prompt") or "", finish, fields.get("answer_format") or "").inc()
        for finding in filter(None, (fields.get("guardrails") or "").split(";")):
            check, _, rest = finding.partition("=")  # "pii_input=flagged:card": kinds aren't labels, to stay bounded
            self.guardrails.labels(check, rest.partition(":")[0]).inc()

    def observe_grade(self, route: str, prompt: str, outcome: str, score: int | None, cost: float) -> None:
        route = self.route_label(route)
        self.judge_runs.labels(route, outcome).inc()
        if score is not None:
            self.judge_scores.labels(route, prompt).observe(score)
        if cost:
            self.judge_cost.labels(route).inc(cost)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    async def refresh_budgets(self, pool) -> None:
        """Every team's spend this month and its cap, read from PostgreSQL. Shared state, so every replica reports
        the same numbers: dashboards take max() across replicas, not sum()."""
        month = dt.datetime.now(dt.UTC).date().replace(day=1)
        async with pool.connection() as conn:
            rows = await conn.execute(
                """
                select t.name, coalesce(b.spent_usd, 0), t.monthly_budget_usd
                from teams t left join budgets b on b.team_id = t.id and b.month = %s
                """,
                (month,),
            )
            self.budgets = [
                (name, float(spent), None if cap is None else float(cap)) async for name, spent, cap in rows
            ]

    async def keep_budgets_fresh(self, pool) -> None:
        while True:
            try:
                await self.refresh_budgets(pool)
            except db.UNAVAILABLE:
                log.warning("couldn't refresh budget metrics: database unavailable")
            await asyncio.sleep(BUDGET_REFRESH_SECONDS)


class _Breakers:
    """Read at scrape time from the replica's breakers, so it's never stale."""

    def __init__(self, breakers):
        self.breakers = breakers

    def collect(self):
        gauge = GaugeMetricFamily(
            "gateway_breaker_open",
            "1 while a candidate's circuit breaker is open or half-open (skipped or on trial), else 0",
            labels=["provider", "model"],
        )
        for (provider, model), breaker in list(self.breakers.by_candidate.items()):
            gauge.add_metric([provider, model], 0 if breaker.state == "closed" else 1)
        yield gauge


class _Budgets:
    def __init__(self, metrics: Metrics):
        self.metrics = metrics

    def collect(self):
        spent = GaugeMetricFamily("gateway_budget_spent_usd", "A team's spend this month", labels=["team"])
        limit = GaugeMetricFamily("gateway_budget_limit_usd", "A team's monthly cap (absent: no cap)", labels=["team"])
        for team, used, cap in self.metrics.budgets:
            spent.add_metric([team], used)
            if cap is not None:
                limit.add_metric([team], cap)
        yield spent
        yield limit


@contextlib.contextmanager
def serving(metrics: Metrics, port: int | None, addr: str):
    """The /metrics endpoint on its own port, in a thread, for as long as the app runs. No port: not served."""
    if port is None:
        yield
        return
    server, thread = start_http_server(port, addr, registry=metrics.registry)
    log.info("metrics on http://%s:%d/metrics", addr, port)
    try:
        yield
    finally:
        server.shutdown()  # stops the serving loop
        server.server_close()  # and frees the port: without it, connections hang instead of being refused
        thread.join(timeout=5)
