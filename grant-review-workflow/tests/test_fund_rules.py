"""Hand-checked boundaries for every rule. These rules both label the gold set and run in the workflow, so
they're tested against expectations written here, not against themselves."""

import pytest

from fund_rules import RULES, failed_rules

ELIGIBLE = {
    "total_revenue_current_year": 400_000,
    "total_expenses_current_year": 380_000,
    "total_assets_end_of_year": 250_000,
    "total_liabilities_end_of_year": 50_000,
    "tax_period_end": "2023-12-31",
}


def check(amount=20_000, form_type="990", **changes):
    return failed_rules({**ELIGIBLE, **changes}, form_type, amount)


def test_an_eligible_application_fails_nothing():
    assert check() == []


@pytest.mark.parametrize("form_type", ["990EZ", "990PF", "990T"])
def test_other_forms_fail(form_type):
    assert check(form_type=form_type) == ["form_type"]


@pytest.mark.parametrize(
    ("revenue", "ok"),
    [(49_999, False), (50_000, True), (20_000_000, True), (20_000_001, False), (None, False)],
)
def test_revenue_range_includes_both_ends(revenue, ok):
    # Expenses are kept at 100,000 so the request-share rule doesn't interfere.
    result = check(total_revenue_current_year=revenue, total_expenses_current_year=100_000)
    assert ("revenue_range" not in result) == ok


@pytest.mark.parametrize(
    ("period_end", "ok"),
    [("2022-06-29", False), ("2022-06-30", True), ("2025-06-30", True), (None, False)],
)
def test_return_must_have_ended_within_36_months_of_the_round(period_end, ok):
    assert ("return_current" not in check(tax_period_end=period_end)) == ok


@pytest.mark.parametrize(
    ("assets", "liabilities", "ok"),
    [(100, 99, True), (100, 100, False), (100, 101, False), (100, None, True), (None, 0, False)],
)
def test_net_assets_must_be_positive(assets, liabilities, ok):
    result = check(total_assets_end_of_year=assets, total_liabilities_end_of_year=liabilities)
    assert ("net_assets" not in result) == ok


@pytest.mark.parametrize(("amount", "ok"), [(4_999, False), (5_000, True), (50_000, True), (50_001, False)])
def test_request_range_includes_both_ends(amount, ok):
    # Expenses of 1,000,000 keep every amount under 25%.
    assert ("request_range" not in check(amount=amount, total_expenses_current_year=1_000_000)) == ok


@pytest.mark.parametrize(("amount", "ok"), [(25_000, True), (25_001, False)])
def test_request_may_be_at_most_a_quarter_of_expenses(amount, ok):
    assert ("request_share" not in check(amount=amount, total_expenses_current_year=100_000)) == ok


def test_missing_expenses_fail_the_share_rule():
    assert "request_share" in check(total_expenses_current_year=None)


def test_several_failures_are_all_reported_in_rule_order():
    result = check(form_type="990EZ", amount=60_000, total_revenue_current_year=10_000, tax_period_end="2020-12-31")
    assert result == ["form_type", "revenue_range", "return_current", "request_range"]
    assert result == sorted(result, key=list(RULES).index)
