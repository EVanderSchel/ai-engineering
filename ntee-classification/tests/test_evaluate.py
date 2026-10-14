"""The shared scorer and the held-out rule, on small hand-checked examples."""

import json

import pytest

import data
import evaluate


def rows(*groups):
    return [{"ein": str(i), "group": g, "mission": f"mission {i}"} for i, g in enumerate(groups)]


def test_accuracy_top3_and_broad_accuracy():
    gold = rows("B", "E", "X", "P")
    ranked = [
        ["B", "O", "P"],  # right
        ["F", "E", "G"],  # wrong group, right broad category (Health), in top 3
        ["P", "S", "T"],  # wrong, not in top 3, different broad category
        ["O", "B", "P"],  # wrong group, right broad category (Human Services), in top 3
    ]
    m = evaluate.score(gold, ranked)
    assert m["accuracy"] == 0.25
    assert m["top3"] == 0.75
    assert m["broad_accuracy"] == 0.75


def test_macro_f1_weights_every_group_equally():
    """Ignoring a rare group costs a lot of macro-F1 even when accuracy stays high."""
    gold = rows(*["B"] * 9, "V")
    always_b = [["B"]] * 10
    m = evaluate.score(gold, always_b)
    assert m["accuracy"] == 0.9
    assert m["macro_f1"] == pytest.approx((2 * 0.9 / 1.9 + 0) / 2)  # B: P 0.9, R 1.0; V: 0
    assert m["per_group"]["V"]["recall"] == 0


def test_empty_predictions_count_as_wrong():
    assert evaluate.score(rows("B"), [[]])["accuracy"] == 0


def test_confusions_are_counted_gold_to_predicted():
    m = evaluate.score(rows("X", "X", "B"), [["B"], ["B"], ["B"]])
    assert m["top_confusions"] == [{"gold": "X", "predicted": "B", "count": 2}]


def test_length_mismatch_is_an_error():
    with pytest.raises(ValueError):
        evaluate.score(rows("B", "E"), [["B"]])


def test_record_saves_predictions_and_appends_history(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evaluate, "HISTORY_PATH", tmp_path / "history.csv")
    for _ in range(2):
        evaluate.record("toy", "dev", rows("B", "E"), [["B", "E"], ["B", "E"]], latency_ms=1.5, cost_per_1k_usd=0.0)
    run = json.loads((tmp_path / "toy_dev.json").read_text(encoding="utf-8"))
    assert run["metrics"]["accuracy"] == 0.5
    assert run["predictions"][1] == {"ein": "1", "gold": "E", "ranked": ["B", "E"]}
    assert len((tmp_path / "history.csv").read_text(encoding="utf-8").splitlines()) == 3  # header + 2 runs


def test_test_split_is_held_out():
    with pytest.raises(data.HeldOutError):
        data.load("test")
    assert data.load("dev") and all(r["split"] == "dev" for r in data.load("dev"))
