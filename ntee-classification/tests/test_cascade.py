"""The cascade simulation, on toy missions with known answers, and on the saved dev runs."""

import pytest

import cascade


def mission(gold, small, confidence, sonnet, ein="1"):
    return {"ein": ein, "gold": gold, "small": small, "confidence": confidence, "small_cost": 0.000001,
            "small_ms": 60.0, "sonnet": sonnet, "sonnet_cost": 0.001, "sonnet_ms": 1000.0}  # fmt: skip


TOY = [
    mission("A", "A", 0.95, "A", "1"),  # confident and right: kept
    mission("B", "C", 0.40, "B", "2"),  # unsure and wrong: Sonnet fixes it
    mission("D", "D", 0.40, "E", "3"),  # unsure but right: Sonnet breaks it
    mission("F", "G", 0.90, "F", "4"),  # confident and wrong: kept, stays wrong
]


def test_the_threshold_decides_who_answers():
    r = cascade.cascade(TOY, 0.5)
    assert r["accuracy"] == 0.5 and r["escalated"] == 0.5
    assert r["cost_per_1k_usd"] == pytest.approx((4 * 0.000001 + 2 * 0.001) / 4 * 1000)
    assert r["mean_ms"] == pytest.approx(60 + 500)


def test_the_extremes_are_each_model_alone():
    assert cascade.cascade(TOY, 0.0)["accuracy"] == 0.5  # small model: A, D right
    assert cascade.cascade(TOY, 1.01)["accuracy"] == cascade.sonnet_alone(TOY)["accuracy"] == 0.75


def test_halves_are_disjoint_and_cover_everything():
    missions = [mission("A", "A", 0.5, "A", str(i)) for i in range(11)]
    tune, held = cascade.halves(missions)
    assert {m["ein"] for m in tune} | {m["ein"] for m in held} == {str(i) for i in range(11)}
    assert not {m["ein"] for m in tune} & {m["ein"] for m in held}


def test_the_dev_report_builds_from_saved_runs():
    missions = cascade.load_missions()
    assert len(missions) == 1813 and all(0 <= m["confidence"] <= 1 for m in missions)
    result = cascade.report(missions)
    assert set(result["chosen_on_tune_half"]) == {"match_sonnet", "most_accurate"}
    assert sum(c["n"] for c in result["calibration"]) == len(missions)
