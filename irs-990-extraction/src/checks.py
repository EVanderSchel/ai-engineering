"""Business-rule checks on an extraction: things a correct reading of page 1 must satisfy.

Part I has totals that the IRS's own e-file validation makes add up (line 12 is the sum of lines 8-11,
and so on), so if Claude's numbers don't add up, it misread at least one of them. Every rule here holds
on all 60 answer keys in the gold set (tests/test_checks.py), so a failure means a misreading, never a
real filing. A rule that real filings break would only cause pointless retries: "independent voting
members <= voting members" sounds safe but one gold filing breaks it, so it isn't here.

A blank line counts as 0 in the arithmetic, the way the form does.
"""

import datetime

YEARS = ("prior_year", "current_year")
REVENUE_LINES = ("contributions_and_grants", "program_service_revenue", "investment_income", "other_revenue")
EXPENSE_LINES = (
    "grants_paid",
    "benefits_paid_to_members",
    "salaries_and_benefits",
    "professional_fundraising_fees",
    "other_expenses",
)  # line 16b (total fundraising expenses) is a memo line, not part of line 18


def _n(answer: dict, name: str) -> int:
    return answer[name] or 0


def _sum_rule(answer: dict, total: str, parts: list[str], description: str) -> str | None:
    expected = sum(_n(answer, p) for p in parts)
    if _n(answer, total) != expected:
        return f"{description}: {total} is {answer[total]}, but the lines it totals add up to {expected}"
    return None


def problems(answer: dict) -> list[str]:
    """Rules the answer breaks, as sentences Claude can act on. Empty when everything checks out."""
    found = []

    ein = answer["ein"]
    if ein is None or len(ein) != 9 or not ein.isdigit():
        found.append(f"Box D: ein is {ein!r}, but an EIN is exactly 9 digits (no dash)")

    begin, end = answer["tax_period_begin"], answer["tax_period_end"]
    if begin and end and datetime.date.fromisoformat(begin) >= datetime.date.fromisoformat(end):
        found.append(f"Line A: the tax year begins {begin}, which is not before it ends {end}")

    for year in YEARS:
        found.append(
            _sum_rule(answer, f"total_revenue_{year}", [f"{n}_{year}" for n in REVENUE_LINES], "Line 12 = lines 8-11")
        )
        found.append(
            _sum_rule(
                answer,
                f"total_expenses_{year}",
                [f"{n}_{year}" for n in EXPENSE_LINES],
                "Line 18 = lines 13-15, 16a and 17",
            )
        )
        expected = _n(answer, f"total_revenue_{year}") - _n(answer, f"total_expenses_{year}")
        if _n(answer, f"revenue_less_expenses_{year}") != expected:
            found.append(
                f"Line 19 = line 12 - line 18: revenue_less_expenses_{year} is "
                f"{answer[f'revenue_less_expenses_{year}']}, but line 12 - line 18 is {expected}"
            )

    for column in ("beginning_of_year", "end_of_year"):
        expected = _n(answer, f"total_assets_{column}") - _n(answer, f"total_liabilities_{column}")
        if _n(answer, f"net_assets_{column}") != expected:
            found.append(
                f"Line 22 = line 20 - line 21: net_assets_{column} is {answer[f'net_assets_{column}']}, "
                f"but line 20 - line 21 is {expected}"
            )

    return [p for p in found if p]
