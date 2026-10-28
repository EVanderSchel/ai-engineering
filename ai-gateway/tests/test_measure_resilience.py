"""The measurement script's own logic: the summaries and the timeline phases it reports."""

import pytest

import measure_resilience as m


def result(ok=True, seconds=0.05, provider="primary", attempts=1, code=None):
    return {"ok": ok, "status": 200 if ok else 503, "seconds": seconds, "provider": provider, "attempts": attempts,
            "code": code}  # fmt: skip


def test_summary_counts_served_backup_and_failures():
    s = m.summarize([
        result(), result(provider="backup", seconds=0.2, attempts=3),
        result(ok=False, provider=None, code="all_candidates_failed"),
        result(ok=False, provider=None, code="all_candidates_failed"),
    ])  # fmt: skip
    assert (s["requests"], s["served"], s["served_pct"], s["by_backup"]) == (4, 2, 50.0, 1)
    assert s["failures"] == {"all_candidates_failed": 2}
    assert s["p50_ms"] == pytest.approx(125.0)


def test_latency_percentiles_use_served_requests_only_but_the_slowest_counts_all():
    s = m.summarize([result(seconds=0.01), result(ok=False, seconds=5.0, code="timeout")])
    assert s["p95_ms"] == 10.0
    assert s["max_ms"] == 5000.0
    assert s["over_deadline"] == 1


def test_nothing_served_has_no_latency():
    s = m.summarize([result(ok=False, code="x")])
    assert s["p50_ms"] is None and s["served_pct"] == 0.0


def test_percentile():
    assert m.percentile([1, 2, 3, 4, 5], 50) == 3
    assert m.percentile([1, 2, 3, 4, 5], 100) == 5


def test_timeline_collapses_runs_of_the_same_state():
    point = {"t": 0.0, "ok": True, "provider": "primary", "attempts": 1, "ms": 20.0, "breaker": "closed"}
    timeline = [
        point, {**point, "t": 0.1},
        {**point, "t": 1.0, "provider": "backup", "attempts": 3, "ms": 90.0},
        {**point, "t": 1.2, "provider": "backup", "attempts": 2, "ms": 25.0, "breaker": "open"},
    ]  # fmt: skip
    phases = m.timeline_phases(timeline)
    assert [(p["breaker"], p["answered_by"], p["requests"]) for p in phases] == [
        ("closed", "primary", 2), ("closed", "backup", 1), ("open", "backup", 1),
    ]  # fmt: skip
    assert phases[0]["until_s"] == 0.1


def test_every_scenario_builds_an_app_every_way():
    for scenario in m.SCENARIOS:
        for mode in m.MODES:
            assert len(m.make_app(scenario, mode).state.providers) == 2


def test_the_report_has_a_row_per_scenario_and_mode():
    s = m.summarize([result()])
    summary = {
        "scenarios": {"healthy": {"description": "", "gateway": s, "one candidate": s, "direct": s}},
        "timeline": m.timeline_phases([{"t": 0, "ok": True, "provider": "primary", "attempts": 1, "ms": 1,
                                         "breaker": "closed"}]),
    }  # fmt: skip
    text = m.report(summary)
    assert "| healthy | gateway |" in text and "|  | direct |" in text
    assert "## Recovery timeline" in text
