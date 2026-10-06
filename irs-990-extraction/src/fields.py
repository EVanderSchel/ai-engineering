"""The fields we extract: Form 990, Part I (Summary), plus the filer's identity from the return header.

This list is the single definition of what the project extracts. The answer keys are built from it
(each field's value read from the IRS e-file XML), and the extraction schema and the eval use the same
names. Element names were checked against a real 2024 e-filed return.

A field whose element is absent from the XML was left blank on the form; its answer is None, which is
different from 0 (an explicit zero on the form).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str  # our name, used in answer keys, the schema, and eval reports
    xml_tag: str  # element name in the IRS e-file XML
    kind: str  # "int", "str", or "date"
    line: str  # where it appears on the form, for humans reading reports
    description: str


# From ReturnHeader (selected by full path: the first BusinessNameLine1Txt in a file can be the paid
# preparer's firm, not the filer). A long name is split over BusinessNameLine1Txt and
# BusinessNameLine2Txt, printed as two lines in box C; reading the whole BusinessName element joins them.
HEADER_FIELDS = [
    Field("ein", "Filer/EIN", "str", "Box D", "Employer identification number (9 digits)"),
    Field("organization_name", "Filer/BusinessName", "str", "Box C", "Name of the organization"),
    Field("tax_period_begin", "TaxPeriodBeginDt", "date", "Line A", "First day of the tax year"),
    Field("tax_period_end", "TaxPeriodEndDt", "date", "Line A", "Last day of the tax year"),
]


def _py_cy(name: str, tag: str, line: str, description: str) -> list[Field]:
    """A Part I line with Prior Year and Current Year columns."""
    return [
        Field(f"{name}_prior_year", f"PY{tag}", "int", f"Part I line {line}, Prior Year", description),
        Field(f"{name}_current_year", f"CY{tag}", "int", f"Part I line {line}, Current Year", description),
    ]


def _boy_eoy(name: str, tag: str, line: str, description: str) -> list[Field]:
    """A Part I line with Beginning of Year and End of Year columns."""
    return [
        Field(
            f"{name}_beginning_of_year", f"{tag}BOYAmt", "int", f"Part I line {line}, Beginning of Year", description
        ),
        Field(f"{name}_end_of_year", f"{tag}EOYAmt", "int", f"Part I line {line}, End of Year", description),
    ]


# From ReturnData/IRS990, in the order they appear in Part I.
PART_I_FIELDS = [
    Field("mission", "ActivityOrMissionDesc", "str", "Part I line 1", "Mission or most significant activities"),
    Field(
        "voting_members",
        "VotingMembersGoverningBodyCnt",
        "int",
        "Part I line 3",
        "Voting members of the governing body",
    ),
    Field(
        "independent_voting_members",
        "VotingMembersIndependentCnt",
        "int",
        "Part I line 4",
        "Independent voting members",
    ),
    Field("employees", "TotalEmployeeCnt", "int", "Part I line 5", "Individuals employed in the calendar year"),
    Field("volunteers", "TotalVolunteersCnt", "int", "Part I line 6", "Volunteers (estimate if necessary)"),
    Field(
        "unrelated_business_revenue", "TotalGrossUBIAmt", "int", "Part I line 7a", "Total unrelated business revenue"
    ),
    Field(
        "unrelated_business_taxable_income",
        "NetUnrelatedBusTxblIncmAmt",
        "int",
        "Part I line 7b",
        "Net unrelated business taxable income",
    ),
    *_py_cy("contributions_and_grants", "ContributionsGrantsAmt", "8", "Contributions and grants"),
    *_py_cy("program_service_revenue", "ProgramServiceRevenueAmt", "9", "Program service revenue"),
    *_py_cy("investment_income", "InvestmentIncomeAmt", "10", "Investment income"),
    *_py_cy("other_revenue", "OtherRevenueAmt", "11", "Other revenue"),
    *_py_cy("total_revenue", "TotalRevenueAmt", "12", "Total revenue"),
    *_py_cy("grants_paid", "GrantsAndSimilarPaidAmt", "13", "Grants and similar amounts paid"),
    *_py_cy("benefits_paid_to_members", "BenefitsPaidToMembersAmt", "14", "Benefits paid to or for members"),
    *_py_cy(
        "salaries_and_benefits", "SalariesCompEmpBnftPaidAmt", "15", "Salaries, other compensation, employee benefits"
    ),
    *_py_cy("professional_fundraising_fees", "TotalProfFndrsngExpnsAmt", "16a", "Professional fundraising fees"),
    Field(
        "total_fundraising_expenses_current_year",
        "CYTotalFundraisingExpenseAmt",
        "int",
        "Part I line 16b",
        "Total fundraising expenses (current year only)",
    ),
    *_py_cy("other_expenses", "OtherExpensesAmt", "17", "Other expenses"),
    *_py_cy("total_expenses", "TotalExpensesAmt", "18", "Total expenses"),
    *_py_cy("revenue_less_expenses", "RevenuesLessExpensesAmt", "19", "Revenue less expenses"),
    *_boy_eoy("total_assets", "TotalAssets", "20", "Total assets"),
    *_boy_eoy("total_liabilities", "TotalLiabilities", "21", "Total liabilities"),
    *_boy_eoy("net_assets", "NetAssetsOrFundBalances", "22", "Net assets or fund balances"),
]

ALL_FIELDS = HEADER_FIELDS + PART_I_FIELDS
FIELD_NAMES = [f.name for f in ALL_FIELDS]


# Part VII, Section A: one row per officer, director, trustee, key employee, or highly paid employee
# (Form990PartVIISectionAGrp). Long lists continue on "Additional Data" pages later in the return.
# Paths are relative to the row element. A checkbox is True when ticked ("X" in the XML) and False when
# not; it's never blank. "name" is a person (PersonNm) or, for some rows, an organization (BusinessName).
PART_VII_COLUMNS = [
    Field("name", "PersonNm|BusinessName", "str", "Part VII-A column (A)", "Name of the person or organization"),
    Field("title", "TitleTxt", "str", "Part VII-A column (A)", "Title"),
    Field("hours", "AverageHoursPerWeekRt", "decimal", "Part VII-A column (B)", "Average hours per week"),
    Field(
        "hours_related",
        "AverageHoursPerWeekRltdOrgRt",
        "decimal",
        "Part VII-A column (B), below the line",
        "Average hours per week for related organizations",
    ),
    Field(
        "director", "IndividualTrusteeOrDirectorInd", "bool", "Part VII-A column (C)", "Individual trustee or director"
    ),
    Field("institutional_trustee", "InstitutionalTrusteeInd", "bool", "Part VII-A column (C)", "Institutional trustee"),
    Field("officer", "OfficerInd", "bool", "Part VII-A column (C)", "Officer"),
    Field("key_employee", "KeyEmployeeInd", "bool", "Part VII-A column (C)", "Key employee"),
    Field(
        "highest_compensated",
        "HighestCompensatedEmployeeInd",
        "bool",
        "Part VII-A column (C)",
        "Highest compensated employee",
    ),
    Field("former", "FormerOfcrDirectorTrusteeInd", "bool", "Part VII-A column (C)", "Former officer or director"),
    Field(
        "pay",
        "ReportableCompFromOrgAmt",
        "int",
        "Part VII-A column (D)",
        "Reportable compensation from the organization",
    ),
    Field(
        "pay_related",
        "ReportableCompFromRltdOrgAmt",
        "int",
        "Part VII-A column (E)",
        "Reportable compensation from related organizations",
    ),
    Field("other_pay", "OtherCompensationAmt", "int", "Part VII-A column (F)", "Estimated other compensation"),
]

# Part VII, Section A, lines 1d and 2: the totals of the columns above (a completeness check on the rows)
# and how many people received more than $100,000. Paths are relative to ReturnData/IRS990.
PART_VII_TOTALS = [
    Field("total_pay", "TotalReportableCompFromOrgAmt", "int", "Part VII-A line 1d, column (D)", "Total of column (D)"),
    Field(
        "total_pay_related",
        "TotReportableCompRltdOrgAmt",
        "int",
        "Part VII-A line 1d, column (E)",
        "Total of column (E)",
    ),
    Field(
        "total_other_pay", "TotalOtherCompensationAmt", "int", "Part VII-A line 1d, column (F)", "Total of column (F)"
    ),
    Field(
        "people_over_100k",
        "IndivRcvdGreaterThan100KCnt",
        "int",
        "Part VII-A line 2",
        "Individuals who received more than $100,000 of reportable compensation",
    ),
]
