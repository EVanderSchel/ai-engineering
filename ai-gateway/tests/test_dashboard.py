"""The Grafana dashboard and the alert rules (step 7 stage 4) only ask for metrics the gateway exports, and the
committed dashboard is current with its generator. A renamed metric would otherwise leave an empty chart, or an alert
that can never fire, without any error."""

import json
import re
import sys

import yaml
from conftest import routes

import metrics
import router
from paths import ROOT

sys.path.insert(0, str(ROOT / "observability" / "grafana"))
import make_dashboard  # noqa: E402

METRIC = re.compile(r"\bgateway_[a-z_]+")


SUFFIXES = {"counter": ["_total"], "histogram": ["_bucket", "_count", "_sum"], "gauge": [""]}


def exported() -> set[str]:
    """Every series name the gateway can produce. Built from each metric's type, since a labelled metric has no
    samples until it's first used."""
    families = metrics.Metrics(routes(), router.Breakers()).registry.collect()
    return {family.name + suffix for family in families for suffix in SUFFIXES[family.type]}


def test_the_dashboard_is_current_with_its_generator():
    committed = json.loads((ROOT / "observability" / "grafana" / "dashboards" / "gateway.json").read_text("utf-8"))
    assert committed == make_dashboard.dashboard(), "regenerate: uv run python observability/grafana/make_dashboard.py"


def test_every_chart_asks_for_metrics_the_gateway_exports():
    queries = [t["expr"] for p in make_dashboard.dashboard()["panels"] for t in p["targets"]]
    names = {m for q in queries for m in METRIC.findall(q)}
    assert names and names <= exported(), names - exported()


def test_every_alert_rule_asks_for_metrics_the_gateway_exports():
    rules = yaml.safe_load((ROOT / "observability" / "alerts.yml").read_text("utf-8"))["groups"][0]["rules"]
    names = {m for r in rules for m in METRIC.findall(r["expr"])}
    assert {r["alert"] for r in rules} >= {"GatewayHighErrorRate", "GatewayHighLatency", "GatewayBreakerOpen",
                                           "GatewayCostSpike"}  # the design's four  # fmt: skip
    assert names <= exported(), names - exported()


def test_panels_fit_grafanas_grid_without_overlapping():
    taken = set()
    for p in make_dashboard.dashboard()["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24
        cells = {(x, y) for x in range(g["x"], g["x"] + g["w"]) for y in range(g["y"], g["y"] + g["h"])}
        assert not cells & taken, p["title"]
        taken |= cells
