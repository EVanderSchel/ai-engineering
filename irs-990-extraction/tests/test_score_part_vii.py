import json

import pytest

import score_part_vii as score

TOTALS = {"total_pay": 90000, "total_pay_related": 0, "total_other_pay": 5000, "people_over_100k": 0}


def row(name, **values):
    base = {
        "name": name,
        "title": "DIRECTOR",
        "hours": 1.0,
        "hours_related": None,
        "director": True,
        "institutional_trustee": False,
        "officer": False,
        "key_employee": False,
        "highest_compensated": False,
        "former": False,
        "pay": 0,
        "pay_related": 0,
        "other_pay": 0,
    }
    return base | values


@pytest.fixture
def key():
    rows = [row("DR ALAN BENSON"), row("MARIA LOPEZ", pay=90000, other_pay=5000), row("JOHN SMITH"), row("JOHN SMITH")]
    return {"rows": rows, "totals": dict(TOTALS)}


def test_a_perfect_answer_has_no_errors(key):
    result = score.score_return(key, json.loads(json.dumps(key)))
    assert result == {"people": 4, "matched": 4, "missed": [], "invented": [], "errors": []}


def test_names_match_ignoring_case_spaces_and_punctuation(key):
    got = json.loads(json.dumps(key))
    got["rows"][0]["name"] = "Dr Alan  Benson"
    assert score.score_return(key, got)["errors"] == []  # case and spaces never count
    got["rows"][0]["name"] = "Dr. Alan Benson"
    result = score.score_return(key, got)
    assert result["matched"] == 4  # still recognized as the same person...
    assert [(e["field"], e["error"]) for e in result["errors"]] == [("name", "wrong_value")]  # ...but "." counts


def test_a_misspelled_name_still_matches_but_the_name_is_wrong(key):
    got = json.loads(json.dumps(key))
    got["rows"][1]["name"] = "MARIA LOPES"
    result = score.score_return(key, got)
    assert result["matched"] == 4 and result["missed"] == []
    assert [(e["field"], e["error"]) for e in result["errors"]] == [("name", "wrong_value")]


def test_missed_and_invented_people(key):
    got = json.loads(json.dumps(key))
    got["rows"] = got["rows"][:2] + [row("JOHN SMITH"), row("ZELDA QUINN")]  # one JOHN SMITH lost, one made up
    result = score.score_return(key, got)
    assert result["missed"] == ["JOHN SMITH"] and result["invented"] == ["ZELDA QUINN"]


def test_row_fields_use_the_part_i_error_types(key):
    got = json.loads(json.dumps(key))
    got["rows"][0]["hours_related"] = 0.0  # blank on the form
    got["rows"][1]["other_pay"] = 500  # misread
    got["rows"][2]["director"] = False  # missed checkbox
    got["totals"]["total_pay_related"] = None  # a printed 0 read as blank
    errors = {(e["row"], e["field"]): e["error"] for e in score.score_return(key, got)["errors"]}
    assert errors == {
        ("DR ALAN BENSON", "hours_related"): "blank_as_zero",
        ("MARIA LOPEZ", "other_pay"): "wrong_value",
        ("JOHN SMITH", "director"): "wrong_value",
        ("(totals)", "total_pay_related"): "zero_as_blank",
    }


def test_no_answer_misses_everyone(key):
    result = score.score_return(key, None)
    assert result["matched"] == 0 and len(result["missed"]) == 4


def test_a_run_is_scored_and_recorded(key, tmp_path, monkeypatch):
    gold = tmp_path / "gold"
    gold.mkdir()
    (gold / "111.json").write_text(json.dumps({"part_vii": key}), encoding="utf-8")
    monkeypatch.setattr(score, "GOLD_DIR", gold)
    monkeypatch.setattr(score, "HISTORY", tmp_path / "history.csv")
    run = tmp_path / "20261006-120000_claude-sonnet-5_v1"
    run.mkdir()
    got = json.loads(json.dumps(key))
    got["rows"].pop()  # one person missed
    record = {
        "object_id": "111",
        "answer": got,
        "problems": [],
        "attempts": [{}, {}],
        "model": "claude-sonnet-5",
        "prompt": "extract_part_vii/v1",
        "prompt_sha256": "abc",
        "cost_usd": 0.05,
        "seconds": 20.0,
    }
    (run / "111.json").write_text(json.dumps(record), encoding="utf-8")
    result = score.score_run(run)
    assert (result["people"], result["missed_people"], result["invented_people"]) == (4, 1, 0)
    assert result["field_accuracy"] == 1.0 and result["totals_correct"] == "4/4"
    assert result["returns_all_correct"] == 0 and result["retries"] == 1
    score.record(result)
    score.record(result)
    assert len((tmp_path / "history.csv").read_text(encoding="utf-8").splitlines()) == 2  # header + one row
