"""extract, without AWS: the request it sends (checked against Bedrock's API definition by the Stubber), the
page rendering, and how a transcription becomes an Extraction."""

import datetime as dt

import boto3
import pymupdf
import pytest
from botocore.stub import ANY, Stubber

import bedrock
import extract
from state import ReturnFigures

PAGE = {
    "form_type": "990",
    "ein_as_printed": "41-1657792",
    "organization_name": "Valley Crest Riders Inc",
    "tax_period_end": "2021-12-31",
    "employees": 1,
    "contributions_and_grants_current_year": 20471,
    "program_service_revenue_current_year": 0,
    "investment_income_current_year": None,
    "other_revenue_current_year": 68934,
    "total_revenue_current_year": 89405,
    "total_expenses_current_year": 104184,
    "total_assets_end_of_year": 50000,
    "total_liabilities_end_of_year": None,
}


@pytest.fixture
def pdf(tmp_path):
    """A one-page, letter-size PDF."""
    path = tmp_path / "return.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=612, height=792)
        doc.save(path)
    return path


def test_the_page_is_rendered_with_its_longer_side_at_1568_px(pdf):
    image = pymupdf.Pixmap(extract.page_png(pdf))
    assert max(image.width, image.height) == pytest.approx(1568, abs=1)


def test_every_figure_read_is_a_figure_the_state_holds():
    read = set(extract.PageOne.model_fields) - {"form_type", "ein_as_printed"}
    assert read <= set(ReturnFigures.model_fields)


def test_the_dash_is_removed_in_code_and_blank_stays_blank():
    extraction = extract.to_extraction(extract.PageOne.model_validate(PAGE))
    figures = extraction.figures
    assert figures.ein == "411657792"
    assert figures.tax_period_end == dt.date(2021, 12, 31)
    assert figures.investment_income_current_year is None  # blank, not 0
    assert figures.program_service_revenue_current_year == 0
    assert extraction.form_type == "990"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),  # 20,471 + 0 + blank + 68,934 = 89,405
        ({"other_revenue_current_year": 68000}, False),
        ({"total_revenue_current_year": None}, None),
    ],
)
def test_revenue_lines_must_add_up_to_total_revenue(changes, expected):
    extraction = extract.to_extraction(extract.PageOne.model_validate({**PAGE, **changes}))
    assert extraction.revenue_adds_up is expected


def test_extract_sends_the_page_image_and_forces_the_page_one_tool(pdf):
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=bedrock.REGION)
    client = session.client("bedrock-runtime")
    png = extract.page_png(pdf)
    tool_use = {"toolUse": {"toolUseId": "t1", "name": "PageOne", "input": PAGE}}
    with Stubber(client) as stubber:
        stubber.add_response(
            "converse",
            {
                "output": {"message": {"role": "assistant", "content": [tool_use]}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 2500, "outputTokens": 300, "totalTokens": 2800},
                "metrics": {"latencyMs": 4000},
            },
            expected_params={
                "modelId": "us.anthropic.claude-sonnet-4-6",
                "system": [{"text": extract.SYSTEM}],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"image": {"format": "png", "source": {"bytes": png}}},
                            {"text": "Transcribe page 1 of this return."},
                        ],
                    }
                ],
                "inferenceConfig": {"maxTokens": extract.MAX_TOKENS},
                "toolConfig": ANY,
            },
        )
        extraction, record = extract.extract(client, pdf)
    assert extraction.figures.total_revenue_current_year == 89405 and extraction.revenue_adds_up
    assert record.step == "extract" and record.input_tokens == 2500


def test_comparison_with_the_answer_key_names_each_wrong_field():
    extraction = extract.to_extraction(extract.PageOne.model_validate(PAGE))
    key = {**extraction.figures.model_dump(mode="json"), "investment_income_current_year": 0}
    assert extract.compare(extraction, key) == ["investment_income_current_year: read None, key 0"]


@pytest.mark.parametrize(
    ("read", "right"),
    [
        ("Harkers Island Fire & Rescue Dept Inc", True),
        ("THE HARKERS ISLAND FIRE AND RESCUE DEPT", True),
        ("Harkers Island Fire Rescue Department", False),
        (None, False),
    ],
)
def test_the_return_name_is_scored_the_way_names_are_compared(read, right):
    extraction = extract.to_extraction(extract.PageOne.model_validate(PAGE))
    extraction.figures.organization_name = read
    assert (
        extract.name_read_right(extraction, {"organization_name": "Harkers Island Fire and Rescue Dept Inc"}) is right
    )
