"""The Prairie Commons Fund's eligibility rules (fictional fund; see docs/design.md).

Plain code on purpose: an automatic decline may only come from these rules, never from a model. The same
function labels the gold set (from the IRS answer keys) and runs in the workflow's eligibility step (from
extracted figures), so tests/test_fund_rules.py checks every boundary by hand: a bug here would otherwise
show up in both places and the eval would never notice.
"""

import datetime as dt

ROUND_CLOSES = dt.date(2025, 6, 30)
# A return counts as current if its tax year ended within 36 months of the round's close.
OLDEST_TAX_PERIOD_END = dt.date(2022, 6, 30)

REVENUE_MIN = 50_000
REVENUE_MAX = 20_000_000
REQUEST_MIN = 5_000
REQUEST_MAX = 50_000
MAX_SHARE_OF_EXPENSES = 0.25

ELIGIBLE_FORMS = {"990"}

# The cross-check flags a figure the narrative claims (annual budget, staff) when it's more than this far
# from the return, relative to the return's figure. Applicants round and estimate, so smaller gaps pass.
CLAIM_TOLERANCE = 0.25

FOCUS_AREAS = ["food_security", "health", "education", "housing", "human_services"]


def claim_gap(claimed: float, actual: float) -> float:
    """How far a claimed figure is from the return's, as a share of the return's figure."""
    return abs(claimed - actual) / actual


def claim_disagrees(claimed: float, actual: float) -> bool:
    return claim_gap(claimed, actual) > CLAIM_TOLERANCE


# Rule IDs, in the order a decline letter lists them.
RULES = {
    "form_type": "Files the full Form 990 (not 990-EZ or 990-PF)",
    "revenue_range": f"Total revenue between ${REVENUE_MIN:,} and ${REVENUE_MAX:,} in the latest year",
    "return_current": f"Latest return's tax year ended on or after {OLDEST_TAX_PERIOD_END.isoformat()}",
    "net_assets": "Positive net assets (total assets minus total liabilities) at year end",
    "request_range": f"Request between ${REQUEST_MIN:,} and ${REQUEST_MAX:,}",
    "request_share": f"Request at most {MAX_SHARE_OF_EXPENSES:.0%} of total annual expenses",
}


def failed_rules(figures: dict, form_type: str, amount_requested: int) -> list[str]:
    """Return the IDs of the rules this application fails (empty: eligible).

    `figures` uses the answer-key field names: total_revenue_current_year, total_expenses_current_year,
    total_assets_end_of_year, total_liabilities_end_of_year, tax_period_end (YYYY-MM-DD). A missing figure
    (None) fails the rule that needs it: the workflow can't confirm eligibility without it, and a reviewer
    can correct it.
    """
    failed = []
    if form_type not in ELIGIBLE_FORMS:
        failed.append("form_type")

    revenue = figures.get("total_revenue_current_year")
    if revenue is None or not REVENUE_MIN <= revenue <= REVENUE_MAX:
        failed.append("revenue_range")

    period_end = figures.get("tax_period_end")
    if period_end is None or dt.date.fromisoformat(period_end) < OLDEST_TAX_PERIOD_END:
        failed.append("return_current")

    assets = figures.get("total_assets_end_of_year")
    liabilities = figures.get("total_liabilities_end_of_year")
    if assets is None or (assets - (liabilities or 0)) <= 0:
        failed.append("net_assets")

    if not REQUEST_MIN <= amount_requested <= REQUEST_MAX:
        failed.append("request_range")

    expenses = figures.get("total_expenses_current_year")
    if expenses is None or amount_requested > MAX_SHARE_OF_EXPENSES * expenses:
        failed.append("request_share")

    return failed
