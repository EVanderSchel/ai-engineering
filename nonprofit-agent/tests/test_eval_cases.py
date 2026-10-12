"""The eval's questions and expected answers: well-formed, and every expected figure recomputed from the
frozen API snapshot (evals/snapshot) with the network blocked, so an answer key can't silently go stale."""

import pytest

import propublica
import run_eval
import tools

CASES = run_eval.load_cases()


@pytest.fixture(autouse=True)
def snapshot_only(monkeypatch):
    monkeypatch.setattr(propublica, "CACHE_DIR", run_eval.SNAPSHOT_DIR)

    def no_network(*args, **kwargs):
        raise AssertionError("the expected answers must come from the snapshot, not the network")

    monkeypatch.setattr(propublica.requests, "get", no_network)


def test_cases_are_well_formed():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)) == 22
    for case in CASES:
        assert case["question"].strip()
        assert case["tags"]
        assert case["expected"] or case["rubric"], f"{case['id']} has nothing to grade"
        for e in case["expected"]:
            assert e["kind"] in ("usd", "pct")


def lookup(value, path: str):
    for part in path.split("."):
        value = value[int(part)] if part.isdigit() else value[part]
    return value


@pytest.mark.parametrize(
    "expected",
    [pytest.param(e, id=f"{c['id']}-{i}") for c in CASES for i, e in enumerate(c["expected"])],
)
def test_expected_figure_matches_the_snapshot(expected):
    tool, args, path = expected["source"]
    actual = lookup(getattr(tools, tool)(**args), path)
    if expected["kind"] == "pct":
        assert round(actual * 100, 2) == expected["value"]
    else:
        assert actual == expected["value"]


def test_no_data_cases_really_have_no_data():
    with pytest.raises(tools.ToolError, match="2024 was filed, but this data source has no extracted figures"):
        tools.get_financials("20-4227123", tax_year=2024)  # q15
    with pytest.raises(tools.ToolError, match="No filing with financial data for tax year 2022"):
        tools.get_financials("42-1520979", tax_year=2022)  # q16, q21
    assert tools.get_financials("42-1477858", tax_year=2023)["filings"][0]["officer_compensation"] is None  # q18
    assert tools.search_organizations("hawkeye riverside charitable trust", state="IA")["total_results"] == 0  # q19


def test_regrading_some_cases_keeps_the_other_rows(monkeypatch):
    monkeypatch.setattr(run_eval, "grade_row", lambda client, case, row: row | {"grade": "new"})
    rows = [
        {"prompt_id": "q13", "grade": "old"},
        {"prompt_id": "q01", "grade": "old"},
        {"prompt_id": "q14", "grade": "old"},
    ]
    result = run_eval.regrade(None, rows, {"q13": {}, "q14": {}})
    assert [(r["prompt_id"], r["grade"]) for r in result] == [("q13", "new"), ("q01", "old"), ("q14", "new")]


def test_judge_spending_accumulates_across_regrades(monkeypatch):
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}  # $5.00 at Opus 5 input prices
    monkeypatch.setattr(
        run_eval.grade,
        "grade",
        lambda case, answer, trajectory, client: {
            "grade": {},
            "explanation": {},
            "judge": {"model": "m", "usage": usage},
        },
    )
    row = {"status": "ok", "answer": "a", "trajectory": [], "judge_cost_usd": 1.0}  # graded once before, $1
    regraded = run_eval.grade_row(None, {}, row)
    assert regraded["judge_cost_usd"] == 5.0  # this grading
    assert regraded["judge_spent_usd"] == 6.0  # both gradings


def test_served_model_check():
    run_eval.served_by("claude-sonnet-5", ["claude-sonnet-5", "claude-sonnet-5-20260101"])
    with pytest.raises(RuntimeError, match="served by"):
        run_eval.served_by("claude-sonnet-5", ["claude-sonnet-5", "claude-haiku-4-5"])
