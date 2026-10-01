import pathlib

import pytest

from fields import ALL_FIELDS, FIELD_NAMES
from irs_xml import NotForm990, answer_key

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "georgia_green_2025.xml"


@pytest.fixture(scope="module")
def key():
    return answer_key(FIXTURE.read_bytes())


def test_every_field_is_present_in_the_answer_key(key):
    assert list(key) == FIELD_NAMES


def test_values_match_the_page_image_checked_by_hand(key):
    # Read off page 1 of the IRS image of this return (Georgia Green Industry Association, FY ending 2025-06-30).
    assert key["ein"] == "581524250"
    assert key["organization_name"] == "GEORGIA GREEN INDUSTRY ASSOCIATION INC"  # the filer, not the preparer
    assert (key["tax_period_begin"], key["tax_period_end"]) == ("2024-07-01", "2025-06-30")
    assert key["mission"] == "PROMOTE THE HORTICULTURE INDUSTRY"
    assert key["employees"] == 2
    assert key["contributions_and_grants_current_year"] == 96299
    assert key["total_revenue_prior_year"] == 405731
    assert key["total_revenue_current_year"] == 340580
    assert key["total_expenses_current_year"] == 330597
    assert key["net_assets_end_of_year"] == 189337


def test_blank_lines_are_none_and_explicit_zeros_stay_zero(key):
    assert key["volunteers"] is None  # line 6 is blank on the form
    assert key["other_revenue_prior_year"] is None  # line 11, Prior Year column is blank
    assert key["other_revenue_current_year"] == 0  # line 11, Current Year shows 0


def test_field_names_are_unique_and_kinds_are_known():
    assert len(set(FIELD_NAMES)) == len(FIELD_NAMES)
    assert {f.kind for f in ALL_FIELDS} <= {"int", "str", "date"}


def test_other_return_types_are_rejected():
    ez = b'<Return xmlns="http://www.irs.gov/efile"><ReturnHeader/><ReturnData><IRS990EZ/></ReturnData></Return>'
    with pytest.raises(NotForm990):
        answer_key(ez)
