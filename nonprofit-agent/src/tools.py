"""The agent's tools: plain Python functions over the ProPublica API.

Each returns a small dict meant for the model to read. Raw API responses are long (60+ cryptic fields
per filing, e.g. "totfuncexpns"), and every byte a tool returns is input tokens the model pays for and
must make sense of, so the tools pick the useful fields and give them plain names. Amounts are whole US
dollars as reported; None means "not reported on this form", never 0.

When a request can't be served (bad EIN, unknown organization, no data for a year), a tool raises
ToolError with a message written for the model: what went wrong and what to try instead.
"""

import re

import propublica

FORM_NAMES = {0: "990", 1: "990-EZ", 2: "990-PF"}

# API field -> plain name. The first four are on every form; the rest only on the full Form 990.
FINANCIAL_FIELDS = {
    "totrevenue": "total_revenue",
    "totfuncexpns": "total_expenses",
    "totassetsend": "total_assets",
    "totliabend": "total_liabilities",
    "totcntrbgfts": "contributions_and_grants",
    "totprgmrevnue": "program_service_revenue",
    "compnsatncurrofcr": "officer_compensation",
    "othrsalwages": "other_salaries_and_wages",
    "profndraising": "professional_fundraising_fees",
}


class ToolError(Exception):
    """A request the tool can't serve; the message is shown to the model."""


def normalize_ein(ein: str | int) -> str:
    """EINs are 9 digits, often written with a dash (53-0196605) and sometimes as numbers that lost a
    leading zero (20738229). Returns the 9-digit string."""
    digits = re.sub(r"[\s-]", "", str(ein))
    if not digits.isdigit() or len(digits) > 9:
        raise ToolError(f"{ein!r} is not an EIN (9 digits, e.g. 53-0196605). Use search_organizations to find one.")
    return digits.zfill(9)


def format_ein(ein: str | int) -> str:
    digits = normalize_ein(ein)
    return f"{digits[:2]}-{digits[2:]}"


def _organization(ein: str | int) -> dict:
    ein = normalize_ein(ein)
    try:
        return propublica.organization(ein)
    except propublica.NotFound:
        raise ToolError(f"No organization with EIN {format_ein(ein)}. Use search_organizations to find one.") from None


def search_organizations(query: str, state: str | None = None, page: int = 0) -> dict:
    """Find tax-exempt organizations by name or keyword, optionally in one US state (2-letter code)."""
    if state is not None and not re.fullmatch(r"[A-Za-z]{2}", state):
        raise ToolError(f"state must be a 2-letter code like 'IA', not {state!r}.")
    data = propublica.search(query, state=state.upper() if state else None, page=page)
    return {
        "total_results": data["total_results"],
        "page": data["cur_page"],
        "num_pages": data["num_pages"],
        "organizations": [
            {
                "ein": format_ein(o["ein"]),
                "name": o["name"],
                "city": o["city"],
                "state": o["state"],
                "ntee_code": o["ntee_code"],
            }
            for o in data["organizations"]
        ],
    }


def get_organization(ein: str | int) -> dict:
    """An organization's profile, and which tax years have financial data."""
    data = _organization(ein)
    org = data["organization"]
    with_data = sorted({f["tax_prd_yr"] for f in data["filings_with_data"]}, reverse=True)
    pdf_only = sorted(
        {f["tax_prd_yr"] for f in data["filings_without_data"]} - set(with_data),
        reverse=True,
    )
    return {
        "ein": format_ein(org["ein"]),
        "name": org["name"],
        "city": org["city"],
        "state": org["state"],
        "ntee_code": org["ntee_code"],
        "subsection": f"501(c)({org['subsection_code']})" if org.get("subsection_code") else None,
        "tax_exempt_since": org.get("ruling_date"),
        "years_with_financial_data": with_data,
        "years_filed_without_data": pdf_only,  # filed, but only as a PDF: no figures available here
    }


def _filing_summary(filing: dict) -> dict:
    form = FORM_NAMES.get(filing["formtype"], str(filing["formtype"]))
    summary = {"tax_year": filing["tax_prd_yr"], "tax_period_end": str(filing["tax_prd"]), "form": form}
    for field, name in FINANCIAL_FIELDS.items():
        summary[name] = filing.get(field)
    return summary


def get_financials(ein: str | int, tax_year: int | None = None) -> dict:
    """Financial figures from an organization's filings: every year with data, or one tax year."""
    data = _organization(ein)
    filings = sorted(data["filings_with_data"], key=lambda f: f["tax_prd"], reverse=True)
    if tax_year is not None:
        filings = [f for f in filings if f["tax_prd_yr"] == tax_year]
        if not filings:
            years = sorted({f["tax_prd_yr"] for f in data["filings_with_data"]}, reverse=True)
            raise ToolError(f"No financial data for tax year {tax_year}. Years with data: {years}.")
    return {
        "ein": format_ein(ein),
        "name": data["organization"]["name"],
        "filings": [_filing_summary(f) for f in filings],
    }


def _ratio(numerator, denominator) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 4)


def compute_ratios(ein: str | int, tax_year: int) -> dict:
    """Standard ratios for one tax year, computed in code so the model doesn't do arithmetic, plus the
    change from the previous year with data."""
    filings = get_financials(ein)["filings"]
    by_year = {f["tax_year"]: f for f in filings}
    if tax_year not in by_year:
        raise ToolError(f"No financial data for tax year {tax_year}. Years with data: {sorted(by_year, reverse=True)}.")
    f = by_year[tax_year]
    earlier = [y for y in by_year if y < tax_year]
    previous = by_year[max(earlier)] if earlier else None

    revenue, expenses = f["total_revenue"], f["total_expenses"]
    surplus = revenue - expenses if revenue is not None and expenses is not None else None
    result = {
        "ein": format_ein(ein),
        "tax_year": tax_year,
        "form": f["form"],
        "surplus": surplus,
        "surplus_margin": _ratio(surplus, revenue),  # share of revenue left after expenses
        "liabilities_to_assets": _ratio(f["total_liabilities"], f["total_assets"]),
        "contributions_share_of_revenue": _ratio(f["contributions_and_grants"], revenue),
        "officer_compensation_share_of_expenses": _ratio(f["officer_compensation"], expenses),
        "compared_with_year": previous["tax_year"] if previous else None,
    }
    for name in ("total_revenue", "total_expenses", "total_assets"):
        before = previous[name] if previous else None
        change = f[name] - before if before is not None and f[name] is not None else None
        result[f"{name}_change"] = change
        result[f"{name}_change_pct"] = _ratio(change, before)
    return result
