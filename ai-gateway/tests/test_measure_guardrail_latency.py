"""The latency measurement's own logic: percentiles, the judge's numbers from its saved runs, the report."""

import json

import measure_guardrail_latency as m


def test_stats_are_in_milliseconds():
    assert m.stats_ms([0.001, 0.002, 0.003, 0.004, 0.100]) == {"p50_ms": 3.0, "p95_ms": 100.0}


def test_judge_numbers_come_from_its_saved_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "RESULTS_DIR", tmp_path)
    assert m.judge_from_runs() == {}
    answers = [{"seconds": s, "cost_usd": "0.0001"} for s in (1.0, 1.2, 3.0)]
    (tmp_path / "injection-judge-dev.json").write_text(json.dumps({"answers": answers}))
    assert m.judge_from_runs() == {"calls": 3, "p50_ms": 1200.0, "p95_ms": 3000.0, "cost_per_call_usd": 0.0001}


def test_the_report_has_each_section():
    row = {"p50_ms": 1.0, "p95_ms": 2.0, "added_p50_ms": 0.5}
    stream = {"first_word": {"p50_ms": 300.0, "p95_ms": 310.0}, "total": {"p50_ms": 1100.0, "p95_ms": 1200.0}}
    result = {
        "overhead": {"none": row},
        "streams": {"streamed as it comes": stream},
        "judge": {"calls": 60, "p50_ms": 1260.0, "p95_ms": 2940.0, "cost_per_call_usd": 0.000127},
    }
    text = m.report(result)
    assert "| none | 1.0 ms | 2.0 ms | +0.5 ms |" in text
    assert "| streamed as it comes | 300.0 ms | 1100.0 ms |" in text
    assert "60 real calls" in text


def test_the_prompt_is_about_two_kilobytes_with_an_email_in_it():
    assert 1800 < len(m.PROMPT) < 2600 and "@example.com" in m.PROMPT
