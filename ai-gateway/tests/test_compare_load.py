"""The load comparison's own logic: reading docker stats and k6's summary, and what each gateway adds over the
upstream. No Docker, no k6."""

import pytest

import compare_load


@pytest.mark.parametrize(("text", "value"), [("97.74%", 97.74), (" 0.00% ", 0.0), ("%", 0.0)])
def test_cpu_is_read_as_percent_of_one_core(text, value):
    assert compare_load.parse_cpu(text) == value


@pytest.mark.parametrize(
    ("text", "mib"), [("587.9MiB / 1GiB", 587.9), ("1.2GiB / 2GiB", 1228.8), ("512KiB / 1GiB", 0.5)]
)
def test_memory_is_read_in_mib(text, mib):
    assert compare_load.parse_mem_mib(text) == mib


def k6_summary(p50=200.0, p95=250.0, failed=0.0, dropped=None):
    metrics = {
        "http_reqs": {"count": 800, "rate": 19.97},
        "http_req_failed": {"value": failed},
        "http_req_duration": {"med": p50, "p(95)": p95, "p(99)": p95 + 10, "max": p95 + 50, "avg": p50},
        "http_req_waiting": {"med": p50 - 5, "p(95)": p95 - 5},
    }
    if dropped is not None:
        metrics["dropped_iterations"] = {"count": dropped}
    return {"metrics": metrics}


def test_k6s_summary_is_reduced_to_the_numbers_compared():
    row = compare_load.summarize(k6_summary(failed=0.0125, dropped=3))
    assert row == {"requests": 800, "per_second": 20.0, "failed_pct": 1.25, "dropped": 3, "p50_ms": 200.0,
                   "p95_ms": 250.0, "p99_ms": 260.0, "max_ms": 300.0, "ttfb_p50_ms": 195.0,
                   "ttfb_p95_ms": 245.0}  # fmt: skip
    assert compare_load.summarize(k6_summary())["dropped"] == 0  # k6 leaves the metric out when nothing dropped


def test_overhead_is_measured_against_the_upstream_at_the_same_rate_and_mode():
    runs = [
        {"target": "upstream", "rate": 20, "stream": False, "p50_ms": 200.0, "p95_ms": 210.0},
        {"target": "gateway", "rate": 20, "stream": False, "p50_ms": 228.3, "p95_ms": 311.4},
        {"target": "litellm", "rate": 20, "stream": False, "p50_ms": 225.1, "p95_ms": 344.7},
        {"target": "gateway", "rate": 20, "stream": True, "p50_ms": 900.0, "p95_ms": 1000.0},  # no upstream stream run
    ]
    assert compare_load.overhead(runs) == [
        {"target": "gateway", "rate": 20, "stream": False, "added_p50_ms": 28.3, "added_p95_ms": 101.4},
        {"target": "litellm", "rate": 20, "stream": False, "added_p50_ms": 25.1, "added_p95_ms": 134.7},
    ]


def test_the_report_has_a_row_per_run_and_per_overhead():
    run = {"target": "gateway", "rate": 20, "stream": False, **compare_load.summarize(k6_summary()),
           "cpu_pct": 84.1, "mem_mib": 152.2, "db_cpu_pct": 10.0}  # fmt: skip
    result = {"date": "2026-11-13T00:00:00+00:00", "hold": "40s", "runs": [run],
              "overhead": [{"target": "gateway", "rate": 20, "stream": False, "added_p50_ms": 28.3,
                            "added_p95_ms": 101.4}]}  # fmt: skip
    text = compare_load.report(result)
    assert "| gateway | 20 | plain | 20.0 | 200.0 | 250.0 | 260.0 | 195.0 | 0.0 | 0 | 84.1 | 152.2 |" in text
    assert "| gateway | 20 | plain | 28.3 | 101.4 |" in text


def test_slow_garbage_collections_are_logged_when_asked(caplog):
    import gc
    import logging

    import app

    ours = lambda: [c for c in gc.callbacks if c.__qualname__ == "watch_gc.<locals>.callback"]  # noqa: E731
    app.watch_gc(threshold_ms=-1)  # every collection counts as slow here
    app.watch_gc(threshold_ms=-1)  # a second app in the same process doesn't add a second callback
    try:
        assert len(ours()) == 1
        with caplog.at_level(logging.WARNING, logger="gateway"):
            gc.collect()
        assert any("garbage collection paused the process" in r.message for r in caplog.records)
    finally:
        gc.callbacks[:] = [c for c in gc.callbacks if c not in ours()]
