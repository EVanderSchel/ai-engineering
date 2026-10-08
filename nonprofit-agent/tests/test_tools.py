"""The tools, on real API responses saved in tests/fixtures (trimmed to a few filings)."""

import json
import pathlib

import pytest

import propublica
import tools

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
RED_CROSS = "53-0196605"  # Form 990 filer; fixture has tax years 2023, 2022, 2021 with data
GATES = "56-2618866"  # private foundation, Form 990-PF


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def api(monkeypatch):
    """Serve fixtures instead of calling the API; anything else is an unknown organization."""

    def organization(ein):
        path = FIXTURES / f"organization_{ein}.json"
        if not path.exists():
            raise propublica.NotFound(ein)
        return load(path.name)

    monkeypatch.setattr(propublica, "organization", organization)
    monkeypatch.setattr(propublica, "search", lambda query, state=None, page=0: load("search_food_bank_IA.json"))


# --- EINs -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ein", ["53-0196605", "530196605", 530196605, " 53-0196605 "])
def test_ein_forms_are_normalized(ein):
    assert tools.normalize_ein(ein) == "530196605"


def test_ein_that_lost_its_leading_zero_is_padded():
    assert tools.format_ein(20738229) == "02-0738229"


@pytest.mark.parametrize("ein", ["American Red Cross", "1234567890", ""])
def test_non_eins_are_rejected_with_a_hint(ein):
    with pytest.raises(tools.ToolError, match="search_organizations"):
        tools.normalize_ein(ein)


# --- search_organizations ----------------------------------------------------------------------------


def test_search_returns_compact_results():
    result = tools.search_organizations("food bank", state="ia")
    assert result["total_results"] == 17
    assert result["organizations"][0] == {
        "ein": "42-1177880",
        "name": "Food Bank Of Iowa",
        "city": "Des Moines",
        "state": "IA",
        "ntee_code": "K31Z",
    }


def test_search_rejects_a_state_name():
    with pytest.raises(tools.ToolError, match="2-letter"):
        tools.search_organizations("food bank", state="Iowa")


# --- get_organization -------------------------------------------------------------------------------


def test_organization_profile_lists_years_with_and_without_data():
    org = tools.get_organization("530196605")
    assert org["name"] == "American National Red Cross"
    assert org["subsection"] == "501(c)(3)"
    assert org["years_with_financial_data"] == [2023, 2022, 2021]
    assert 2024 in org["years_filed_without_data"]  # newest filing: PDF only, no figures yet


def test_unknown_organization_is_a_tool_error():
    with pytest.raises(tools.ToolError, match="No organization with EIN 00-0000001"):
        tools.get_organization("000000001")


# --- get_financials ---------------------------------------------------------------------------------


def test_financials_use_plain_names_newest_first():
    filings = tools.get_financials(RED_CROSS)["filings"]
    assert [f["tax_year"] for f in filings] == [2023, 2022, 2021]
    assert filings[0]["form"] == "990"
    assert filings[0]["total_revenue"] == 3217077611
    assert filings[0]["total_expenses"] == 2971106889
    assert filings[0]["officer_compensation"] == 5947262


def test_financials_for_one_year():
    result = tools.get_financials(RED_CROSS, tax_year=2022)
    assert [f["tax_year"] for f in result["filings"]] == [2022]


def test_missing_year_lists_the_years_available():
    with pytest.raises(tools.ToolError, match=r"Years with data: \[2023, 2022, 2021\]"):
        tools.get_financials(RED_CROSS, tax_year=2024)


def test_fields_a_form_does_not_have_are_none_not_zero():
    filing = tools.get_financials(GATES)["filings"][0]
    assert filing["form"] == "990-PF"
    assert filing["total_revenue"] is not None
    assert filing["officer_compensation"] is None
    assert filing["contributions_and_grants"] is None


# --- compute_ratios ---------------------------------------------------------------------------------


def test_ratios_and_change_from_the_previous_year():
    r = tools.compute_ratios(RED_CROSS, 2023)
    assert r["surplus"] == 3217077611 - 2971106889
    assert r["surplus_margin"] == round((3217077611 - 2971106889) / 3217077611, 4)
    assert r["liabilities_to_assets"] == round(1008326202 / 4028321133, 4)
    assert r["contributions_share_of_revenue"] == round(919126379 / 3217077611, 4)
    assert r["compared_with_year"] == 2022
    assert r["total_revenue_change"] == 3217077611 - 3182229338
    assert r["total_revenue_change_pct"] == round((3217077611 - 3182229338) / 3182229338, 4)


def test_oldest_year_has_no_comparison():
    r = tools.compute_ratios(RED_CROSS, 2021)
    assert r["compared_with_year"] is None
    assert r["total_revenue_change"] is None
    assert r["total_revenue_change_pct"] is None


def test_ratios_needing_990_only_fields_are_none_for_a_990_pf():
    r = tools.compute_ratios(GATES, 2023)
    assert r["form"] == "990-PF"
    assert r["surplus_margin"] is not None
    assert r["contributions_share_of_revenue"] is None
    assert r["officer_compensation_share_of_expenses"] is None
