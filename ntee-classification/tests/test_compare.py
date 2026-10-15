"""The step 5 comparison: table rows, payback arithmetic, and that it runs on the saved dev results."""

import math

import pytest

import compare


def test_payback_is_training_cost_over_the_saving_per_prediction():
    # $0.05 training, $0.001 vs $0.72 per 1,000 -> saves $0.000719 each -> about 70 predictions
    assert compare.payback_predictions(0.05, 0.001, 0.72) == pytest.approx(0.05 / 0.000719)


def test_no_payback_when_the_rival_is_cheaper():
    assert math.isinf(compare.payback_predictions(0.05, 1.0, 0.5))


def test_money_formats_small_and_free_costs():
    assert compare.money(0) == "~$0"
    assert compare.money(0.0007) == "$0.0007"
    assert compare.money(0.72) == "$0.72"


def test_the_dev_comparison_builds_from_saved_runs(tmp_path, monkeypatch):
    rows = [compare.row(a, compare.load(a, "dev")) | {"n": 1813} for a in compare.APPROACHES]
    assert {r["kind"] for r in rows} == {"baseline", "lora", "claude"}
    assert compare.table(rows).count("\n") == len(rows) + 1  # header, separator, one line each
    compare.chart(rows, tmp_path / "chart.png", "dev")
    assert (tmp_path / "chart.png").stat().st_size > 10_000
