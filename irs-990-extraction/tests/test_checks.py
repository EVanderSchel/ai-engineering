import json

import pytest

from checks import problems
from paths import GOLD_DIR


def gold_answers() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))["fields"] for p in sorted(GOLD_DIR.glob("*.json"))]


@pytest.fixture
def answer():
    return gold_answers()[0]


def test_every_answer_key_passes_every_check():
    # A rule that a real filing breaks would make correct extractions retry for nothing.
    answers = gold_answers()
    assert len(answers) == 60
    for answer in answers:
        assert problems(answer) == [], answer["ein"]


def test_a_misread_revenue_line_breaks_the_line_12_total(answer):
    answer["investment_income_current_year"] = (answer["investment_income_current_year"] or 0) + 1000
    [found] = problems(answer)
    assert found.startswith("Line 12 = lines 8-11: total_revenue_current_year")


def test_a_lost_minus_sign_breaks_line_19(answer):
    answer["revenue_less_expenses_prior_year"] = -(answer["revenue_less_expenses_prior_year"] or 0) or 5
    assert [p for p in problems(answer) if p.startswith("Line 19")]


def test_net_assets_must_be_assets_minus_liabilities(answer):
    answer["net_assets_end_of_year"] = (answer["net_assets_end_of_year"] or 0) + 1
    [found] = problems(answer)
    assert found.startswith("Line 22")


@pytest.mark.parametrize("ein", ["58-1524250", "58152425", None])
def test_ein_must_be_nine_digits(answer, ein):
    answer["ein"] = ein
    [found] = problems(answer)
    assert found.startswith("Box D")


def test_tax_year_must_begin_before_it_ends(answer):
    answer["tax_period_begin"], answer["tax_period_end"] = answer["tax_period_end"], answer["tax_period_begin"]
    [found] = problems(answer)
    assert found.startswith("Line A")


def test_blank_lines_count_as_zero(answer):
    for name in ("other_revenue_current_year", "investment_income_current_year"):
        answer["total_revenue_current_year"] -= answer[name] or 0
        answer["revenue_less_expenses_current_year"] -= answer[name] or 0
        answer[name] = None
    assert problems(answer) == []
