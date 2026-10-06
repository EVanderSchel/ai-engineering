import pathlib

import pytest

from fields import ALL_FIELDS, FIELD_NAMES, PART_VII_COLUMNS, PART_VII_TOTALS
from irs_xml import NotForm990, answer_key, part_vii

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


def test_a_name_on_two_lines_is_joined():
    # Long names are split over two elements and printed on two lines in box C.
    xml = (
        b'<Return xmlns="http://www.irs.gov/efile"><ReturnHeader><Filer><EIN>123456789</EIN><BusinessName>'
        b"<BusinessNameLine1Txt>ASIAN TASK FORCE AGAINST DOMESTIC</BusinessNameLine1Txt>"
        b"<BusinessNameLine2Txt>VIOLENCE INC</BusinessNameLine2Txt>"
        b"</BusinessName></Filer></ReturnHeader><ReturnData><IRS990/></ReturnData></Return>"
    )
    assert answer_key(xml)["organization_name"] == "ASIAN TASK FORCE AGAINST DOMESTIC VIOLENCE INC"


def test_part_vii_rows_and_totals_from_the_fixture():
    part = part_vii(FIXTURE.read_bytes())
    assert len(part["rows"]) == 29
    assert part["rows"][0] == {
        "name": "STAN DEAL",
        "title": "CHAIRMAN",
        "hours": 4.0,
        "hours_related": 0.0,  # written as 0.00 in this return; other returns leave it blank (None)
        "director": False,
        "institutional_trustee": False,
        "officer": True,
        "key_employee": False,
        "highest_compensated": False,
        "former": False,
        "pay": 0,
        "pay_related": 0,
        "other_pay": 0,
    }
    assert part["totals"] == {"total_pay": 0, "total_pay_related": 0, "total_other_pay": 0, "people_over_100k": 0}


def test_part_vii_reads_checkboxes_hours_blanks_and_organization_names():
    xml = (
        b'<Return xmlns="http://www.irs.gov/efile"><ReturnHeader/><ReturnData><IRS990>'
        b"<Form990PartVIISectionAGrp><PersonNm>JANE DOE</PersonNm><TitleTxt>CEO</TitleTxt>"
        b"<AverageHoursPerWeekRt>37.50</AverageHoursPerWeekRt><OfficerInd>X</OfficerInd>"
        b"<KeyEmployeeInd>X</KeyEmployeeInd><ReportableCompFromOrgAmt>151000</ReportableCompFromOrgAmt>"
        b"<ReportableCompFromRltdOrgAmt>0</ReportableCompFromRltdOrgAmt>"
        b"<OtherCompensationAmt>9000</OtherCompensationAmt></Form990PartVIISectionAGrp>"
        b"<Form990PartVIISectionAGrp><BusinessName><BusinessNameLine1Txt>ACME TRUST</BusinessNameLine1Txt>"
        b"<BusinessNameLine2Txt>COMPANY</BusinessNameLine2Txt></BusinessName><TitleTxt>TRUSTEE</TitleTxt>"
        b"<InstitutionalTrusteeInd>X</InstitutionalTrusteeInd><ReportableCompFromOrgAmt>0</ReportableCompFromOrgAmt>"
        b"<ReportableCompFromRltdOrgAmt>0</ReportableCompFromRltdOrgAmt>"
        b"<OtherCompensationAmt>0</OtherCompensationAmt></Form990PartVIISectionAGrp>"
        b"<TotalReportableCompFromOrgAmt>151000</TotalReportableCompFromOrgAmt>"
        b"<IndivRcvdGreaterThan100KCnt>1</IndivRcvdGreaterThan100KCnt>"
        b"</IRS990></ReturnData></Return>"
    )
    jane, trust = part_vii(xml)["rows"]
    assert (jane["hours"], jane["hours_related"]) == (37.5, None)  # a blank cell is None, not 0
    assert (jane["officer"], jane["key_employee"], jane["director"]) == (True, True, False)  # unticked is False
    assert trust["name"] == "ACME TRUST COMPANY" and trust["institutional_trustee"] is True
    assert part_vii(xml)["totals"] == {
        "total_pay": 151000,
        "total_pay_related": None,  # line 1d left blank
        "total_other_pay": None,
        "people_over_100k": 1,
    }


def test_field_names_are_unique_and_kinds_are_known():
    assert len(set(FIELD_NAMES)) == len(FIELD_NAMES)
    assert {f.kind for f in ALL_FIELDS} <= {"int", "str", "date"}


def test_other_return_types_are_rejected():
    ez = b'<Return xmlns="http://www.irs.gov/efile"><ReturnHeader/><ReturnData><IRS990EZ/></ReturnData></Return>'
    with pytest.raises(NotForm990):
        answer_key(ez)


def test_part_vii_column_kinds_are_known():
    assert {f.kind for f in PART_VII_COLUMNS + PART_VII_TOTALS} <= {"int", "str", "decimal", "bool"}
    assert len({f.name for f in PART_VII_COLUMNS}) == len(PART_VII_COLUMNS)
