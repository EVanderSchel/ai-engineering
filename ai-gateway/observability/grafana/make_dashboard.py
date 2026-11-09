"""Writes observability/grafana/dashboards/gateway.json (run: uv run python observability/grafana/make_dashboard.py).

The gateway's Grafana dashboard (step 7 stage 4), generated so its queries live in reviewable Python rather than
hundreds of lines of JSON, and so a test (tests/test_dashboard.py) can check the committed JSON is current and that
every query names a metric the gateway really exports. Edit here, then regenerate.

Layout, top to bottom: the headline numbers; traffic and errors; latency; providers (attempts, fallbacks,
breakers); money (spend, tokens, budgets); guardrails and firing alerts.
"""

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}

# Shared pieces of PromQL. rate(...[1m]) over a 5 s scrape keeps the charts responsive locally.
# "or vector(0)": with no 5xx at all there's no series to divide, and the panel would say "No data" instead of 0%.
ERRORS = '(sum(rate(gateway_requests_total{status=~"5.."}[5m])) or vector(0)) / sum(rate(gateway_requests_total[5m]))'
ROUTES = 'route!~"|unknown"'  # chat routes only: not the models list, not names a caller made up


def p95(metric: str, matchers: str = "", by: str = "route") -> str:
    return f"histogram_quantile(0.95, sum by ({by}, le) (rate({metric}_bucket{{{matchers}}}[5m])))"


def target(expr: str, legend: str = "", instant: bool = False) -> dict:
    out = {"datasource": PROMETHEUS, "expr": expr, "legendFormat": legend, "refId": "A"}
    if instant:
        out |= {"instant": True, "range": False, "format": "table"}
    return out


def panel(kind: str, title: str, targets: list[dict], w: int, h: int, unit: str = "short", **extra) -> dict:
    for i, t in enumerate(targets):
        t["refId"] = "ABCDEFGH"[i]
    return {
        "type": kind, "title": title, "datasource": PROMETHEUS, "targets": targets,
        "gridPos": {"w": w, "h": h}, "fieldConfig": {"defaults": {"unit": unit}, "overrides": []}, **extra,
    }  # fmt: skip


def stat(title: str, expr: str, unit: str, thresholds: list[tuple[float | None, str]], description: str) -> dict:
    steps = [{"value": value, "color": color} for value, color in thresholds]
    out = panel("stat", title, [target(expr)], w=4, h=4, unit=unit, description=description)
    out["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": steps}
    out["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "area"}
    return out


def series(title: str, targets: list[dict], unit: str = "short", w: int = 12, description: str = "") -> dict:
    out = panel("timeseries", title, targets, w=w, h=8, unit=unit, description=description)
    out["options"] = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
    return out


GREEN, AMBER, RED = "green", "orange", "red"

PANELS = [
    # The headline numbers.
    stat(
        "Requests / s",
        "sum(rate(gateway_requests_total[1m]))",
        "reqps",
        [(None, GREEN)],
        "Everything the gateway answered, refusals included.",
    ),
    stat(
        "Server errors",
        ERRORS,
        "percentunit",
        [(None, GREEN), (0.01, AMBER), (0.05, RED)],
        "Share of requests answered 5xx over 5 minutes. The GatewayHighErrorRate alert fires above 5%.",
    ),
    stat(
        "p95, whole answers",
        p95("gateway_request_duration_seconds", f'stream="false",{ROUTES}', by="le"),
        "s",
        [(None, GREEN), (5, AMBER), (15, RED)],
        "Non-streamed answers, all routes. Alert: above 15 s per route.",
    ),
    stat(
        "p95, first token",
        p95("gateway_first_token_seconds", by="le"),
        "s",
        [(None, GREEN), (2, AMBER), (5, RED)],
        "Streams: the wait for the first word. Alert: above 5 s per route.",
    ),
    stat(
        "In flight",
        "sum(gateway_requests_in_flight)",
        "short",
        [(None, GREEN), (8, AMBER)],
        "Requests being handled now. The CPU-hosted local route queues beyond about 8 (step 6).",
    ),
    stat(
        "Breakers open",
        "sum(max by (provider, model) (gateway_breaker_open)) or vector(0)",
        "short",
        [(None, GREEN), (1, RED)],
        "Provider models being skipped after repeated failures.",
    ),
    # Traffic and errors.
    series(
        "Requests by route and outcome",
        [target(f"sum by (route, outcome) (rate(gateway_requests_total{{{ROUTES}}}[1m]))", "{{route}} {{outcome}}")],
        "reqps",
        description="ok, failed, blocked, stream_error, or refused (never admitted: 4xx).",
    ),
    series(
        "Responses by status", [target("sum by (status) (rate(gateway_requests_total[1m]))", "{{status}}")], "reqps"
    ),
    # Latency.
    series(
        "Whole answers: p50 and p95 by route",
        [
            target(p95("gateway_request_duration_seconds", f'stream="false",{ROUTES}'), "p95 {{route}}"),
            target(
                p95("gateway_request_duration_seconds", f'stream="false",{ROUTES}').replace("0.95", "0.5"),
                "p50 {{route}}",
            ),
        ],
        "s",
    ),
    series(
        "Streams: time to first token, p95 by route and provider",
        [target(p95("gateway_first_token_seconds", by="route, provider"), "{{route}} via {{provider}}")],
        "s",
    ),
    # Providers.
    series(
        "Provider calls by outcome",
        [
            target(
                "sum by (provider, model, outcome) (rate(gateway_provider_attempts_total[1m]))",
                "{{provider}}/{{model}} {{outcome}}",
            )
        ],
        "reqps",
        w=8,
        description="Each try, retries included; skipped = breaker open.",
    ),
    series(
        "Fallbacks",
        [target("sum by (route, provider) (rate(gateway_fallbacks_total[1m]))", "{{route}} answered by {{provider}}")],
        "reqps",
        w=8,
    ),
    series(
        "Breakers (1 = open)",
        [target("max by (provider, model) (gateway_breaker_open)", "{{provider}}/{{model}}")],
        w=8,
    ),
    # Money.
    series(
        "Spend per hour, by team",
        [target("sum by (team) (rate(gateway_cost_usd_total[5m])) * 3600", "{{team}}")],
        "currencyUSD",
        w=8,
        description="At the rate of the last 5 minutes. Alert: an hour over 3x the team's usual.",
    ),
    series(
        "Tokens per minute, by team",
        [target("sum by (team, kind) (rate(gateway_tokens_total[1m])) * 60", "{{team}} {{kind}}")],
        w=8,
    ),
    {
        **panel(
            "bargauge",
            "Budget used this month",
            [target("max by (team) (gateway_budget_spent_usd) / max by (team) (gateway_budget_limit_usd)", "{{team}}")],
            w=8,
            h=8,
            unit="percentunit",
            description="Teams with a cap. Alert: above 90%.",
        ),
        "options": {
            "orientation": "horizontal",
            "displayMode": "gradient",
            "reduceOptions": {"calcs": ["lastNotNull"]},
        },
    },
    # Guardrails and alerts.
    series(
        "Guardrail findings (per 5 minutes)",
        [target("sum by (check, action) (increase(gateway_guardrail_findings_total[5m]))", "{{check}} {{action}}")],
    ),
    # Drift (step 9): live answers against what the eval canary measured.
    series(
        "Answers off format or cut off, against the canary",
        [
            target(
                'sum by (route, prompt) (rate(gateway_answers_total{format="bad"}[15m]))'
                ' / sum by (route, prompt) (rate(gateway_answers_total{format!=""}[15m]))',
                "{{route}} {{prompt}} off format",
            ),
            target(
                'sum by (route, prompt) (rate(gateway_answers_total{format!="",finish="length"}[15m]))'
                ' / sum by (route, prompt) (rate(gateway_answers_total{format!=""}[15m]))',
                "{{route}} {{prompt}} cut off",
            ),
            target("gateway:answer_bad_format_ratio:canary", "{{route}} {{prompt}} off format, canary"),
        ],
        "percentunit",
        description="Answers to registered prompts that declare a format. Alerts (observability/drift.yml): over an "
        "hour, 10 points above the canary's rate.",
    ),
    series(
        "Drift judge: mean grade (1-5) of sampled answers, against the canary",
        [
            target(
                "sum by (route, prompt) (rate(gateway_judge_scores_sum[1h]))"
                " / sum by (route, prompt) (rate(gateway_judge_scores_count[1h]))",
                "{{route}} {{prompt}}",
            ),
            target("gateway:judge_score_mean:canary", "{{route}} {{prompt}}, canary"),
        ],
        description="Routes with drift.judge_rate above 0. The judge has no answer key: it catches broken formats, "
        "refusals and off-task answers better than plausible wrong ones. Alert: 0.5 below the canary over 6 hours.",
    ),
    {
        **panel(
            "table",
            "Alerts firing now",
            [target('ALERTS{alertstate="firing"}', instant=True)],
            w=12,
            h=8,
            description="From Prometheus, which evaluates observability/alerts.yml and drift.yml. Empty is good.",
        ),
        "transformations": [
            {"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True, "__name__": True}}}
        ],
    },
]


def layout(panels: list[dict]) -> list[dict]:
    """Left to right, wrapping at Grafana's 24 columns."""
    x = y = row_height = 0
    for i, p in enumerate(panels, start=1):
        w, h = p["gridPos"]["w"], p["gridPos"]["h"]
        if x + w > 24:
            x, y, row_height = 0, y + row_height, 0
        p["gridPos"] |= {"x": x, "y": y}
        p["id"] = i
        x, row_height = x + w, max(row_height, h)
    return panels


def dashboard() -> dict:
    return {
        "uid": "ai-gateway", "title": "ai-gateway", "tags": ["ai-gateway"], "timezone": "browser",
        "description": "Generated by observability/grafana/make_dashboard.py: edit that, not this file.",
        "editable": False, "refresh": "10s", "time": {"from": "now-30m", "to": "now"},
        "schemaVersion": 39, "version": 1, "panels": layout(PANELS),
    }  # fmt: skip


if __name__ == "__main__":
    path = HERE / "dashboards" / "gateway.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dashboard(), indent=1) + "\n", encoding="utf-8")
    print("wrote", path)
