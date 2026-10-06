import csv
import json

import pytest
from test_checks import gold_answers

import evaluate


@pytest.mark.parametrize(
    ("name", "expected", "got", "result"),
    [
        ("total_revenue_current_year", 340580, 340580, "correct"),
        ("total_revenue_current_year", None, None, "correct"),
        ("volunteers", None, 0, "blank_as_zero"),
        ("other_revenue_current_year", 0, None, "zero_as_blank"),
        ("total_revenue_current_year", 340580, None, "missed"),
        ("volunteers", None, 12, "invented"),
        ("total_revenue_current_year", 340580, 345080, "wrong_value"),
        ("revenue_less_expenses_current_year", -5000, 5000, "wrong_value"),  # a lost minus sign
        ("tax_period_end", "2025-06-30", "2025-06-30", "correct"),
        ("ein", "411657792", "41-1657792", "correct"),  # a dash isn't a digit
        ("ein", "411657792", "416657792", "wrong_value"),  # but every digit counts
        ("organization_name", "CHARLESTON DAY SCHOOLINC", "Charleston Day School Inc", "correct"),
        ("organization_name", "KAPPA SIGMA FRATERNITY 87 GAMMA-GAMMA", "KAPPA SIGMA FRATERNITY", "wrong_value"),
        ("organization_name", "ST. JUDE CENTER", "ST JUDE CENTER", "wrong_value"),  # punctuation counts
        ("mission", "PROMOTE THE\nHORTICULTURE  INDUSTRY", "promote the horticulture industry", "correct"),
    ],
)
def test_outcomes(name, expected, got, result):
    assert evaluate.outcome(name, expected, got) == result


def test_text_fields_exist():
    assert evaluate.TEXT_FIELDS <= set(evaluate.FIELD_NAMES)


@pytest.fixture
def run_dir(tmp_path):
    """A fake run of the first two dev filings: one perfect, one with a blank read as 0 and a bad EIN."""
    keys = evaluate.gold()
    first, second = [object_id for object_id, key in keys.items() if key["split"] == "dev"][:2]
    run = tmp_path / "20261006-120000_claude-sonnet-5_v1"
    run.mkdir()
    wrong = keys[second]["fields"] | {"ein": "000000000"}
    blank = next(name for name, value in wrong.items() if value is None)
    wrong[blank] = 0
    for object_id, answer, attempts in [(first, keys[first]["fields"], 1), (second, wrong, 2)]:
        record = {
            "object_id": object_id,
            "seconds": 10.0 if attempts == 1 else 20.0,
            "cost_usd": 0.02 if attempts == 1 else 0.04,
            "answer": answer,
            "problems": [],
            "model": "claude-sonnet-5",
            "prompt": "extract/v1",
            "prompt_sha256": "abc",
            "attempts": [{}] * attempts,
        }
        (run / f"{object_id}.json").write_text(json.dumps(record), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"run": run.name}), encoding="utf-8")
    return run


def test_a_run_is_scored_per_field_and_by_error_type(run_dir):
    score = evaluate.score_run(run_dir)
    assert score["filings"] == 2 and score["split"] == "dev"
    assert score["field_accuracy"] == round(1 - 2 / 84, 4)
    assert score["filings_all_correct"] == 1
    assert (score["blank_as_zero"], score["wrong_value"], score["missed"]) == (1, 1, 0)
    assert score["per_field"]["ein"]["correct"] == 1
    assert score["retries"] == 1
    assert score["cost_per_filing_usd"] == 0.03 and score["mean_seconds"] == 15.0


def test_no_answer_counts_every_field_as_missed_or_blank(run_dir):
    keys = evaluate.gold()
    with_a_value = 0
    for path in run_dir.glob("2*.json"):  # every filing, so the order glob returns them in doesn't matter
        record = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(record | {"answer": None}), encoding="utf-8")
        with_a_value += sum(value is not None for value in keys[record["object_id"]]["fields"].values())

    score = evaluate.score_run(run_dir)
    assert score["filings_all_correct"] == 0
    assert score["missed"] + score["zero_as_blank"] == with_a_value  # every field that has a value is lost
    assert score["blank_as_zero"] + score["invented"] + score["wrong_value"] == 0  # and blanks stay right


def test_history_keeps_one_row_per_run(run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "HISTORY", tmp_path / "results" / "history.csv")
    score = evaluate.score_run(run_dir)
    evaluate.record(score)
    evaluate.record(score)  # rescoring the same run replaces its row
    with evaluate.HISTORY.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert (
        len(rows) == 1 and rows[0]["run"] == run_dir.name and rows[0]["field_accuracy"] == str(score["field_accuracy"])
    )


def test_answer_keys_cover_the_whole_gold_set():
    keys = evaluate.gold()
    assert len(keys) == len(gold_answers()) == 60
    assert sum(key["split"] == "dev" for key in keys.values()) == 21


def test_only_finished_runs_count_as_complete(run_dir):
    assert evaluate.is_complete(run_dir)  # an older summary without "complete" finished
    (run_dir / "summary.json").write_text(json.dumps({"complete": False}), encoding="utf-8")
    assert not evaluate.is_complete(run_dir)
    (run_dir / "summary.json").unlink()
    assert not evaluate.is_complete(run_dir)  # it crashed before writing one
