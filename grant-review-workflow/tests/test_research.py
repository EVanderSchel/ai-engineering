"""research and the tool-use loop, without network or AWS: ProPublica is replaced by small fixtures shaped like
its API responses, and Bedrock by botocore's Stubber (requests checked against Bedrock's API definition)."""

import json

import boto3
import pytest
from botocore.stub import ANY, Stubber

import bedrock
import propublica
import propublica_tools
import research
from state import ApplicationForm

ORG = {
    "organization": {"ein": 411657792, "name": "VALLEY CREST RIDERS INC", "city": "TOWN", "state": "MN"},
    "filings_with_data": [
        {"tax_prd": 202112, "tax_prd_yr": 2021, "totrevenue": 89405, "totfuncexpns": 104184},
        {"tax_prd": 202012, "tax_prd_yr": 2020, "totrevenue": 76865, "totfuncexpns": 80000},
    ],
    "filings_without_data": [],
}
SEARCH = {
    "total_results": 1,
    "organizations": [{"ein": 123456789, "name": "OTHER CLUB", "city": "ELSEWHERE", "state": "WI"}],
}


@pytest.fixture
def api(monkeypatch):
    """ProPublica answers from fixtures; records which requests were made."""
    seen = []

    def organization(ein):
        seen.append(("organization", ein))
        if ein == "411657792":
            return ORG
        raise propublica.NotFound(ein)

    def search(query, state=None):
        seen.append(("search", query))
        return SEARCH

    monkeypatch.setattr(propublica, "organization", organization)
    monkeypatch.setattr(propublica, "search", search)
    return seen


@pytest.fixture
def stubbed():
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=bedrock.REGION)
    client = session.client("bedrock-runtime")
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def tool_reply(name, tool_input, tool_use_id="t1"):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 900, "outputTokens": 60, "totalTokens": 960},
        "metrics": {"latencyMs": 700},
    }


def form(name="Valley Crest Riders Inc", ein="411657792"):
    return ApplicationForm(ein=ein, organization_name=name, amount_requested=10_000)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Kaji Aso Studio Inc", "KAJI ASO STUDIO INC"),
        ("Benevolent & Protective Order of Elks 2439", "BENEVOLENT AND PROTECTIVE ORDER OF ELKS 2439"),
        ("The Wilds Christian Association, Inc.", "WILDS CHRISTIAN ASSOCIATION"),
        ("Family Crisis Network Corp", "FAMILY CRISIS NETWORK"),
    ],
)
def test_formatting_differences_normalize_away(a, b):
    assert research.normalize_name(a) == research.normalize_name(b)


def test_different_organizations_stay_different():
    assert research.normalize_name("Southwest Brooklyn Industrial Development Corporation") != research.normalize_name(
        "ARAPAHOE COUNTY BAR ASSOCIATION"
    )


def test_an_unknown_ein_is_reported_as_not_found_without_a_model_call(api):
    result, calls, _ = research.research(None, form(ein="999999999"))
    assert result.found is False and calls == []


def test_a_matching_name_needs_no_model_call_and_brings_the_latest_figures(api):
    result, calls, trajectory = research.research(None, form())  # no client: any model call would fail
    assert result.name_matches_form is True and calls == [] and trajectory == []
    assert result.registered_name == "VALLEY CREST RIDERS INC" and result.years_with_figures == [2021, 2020]
    assert (result.latest_year, result.latest_revenue, result.latest_expenses) == (2021, 89405, 104184)


def test_a_name_the_judge_finds_equivalent_matches(api, stubbed):
    client, stubber = stubbed
    verdict = {"same_organization": True, "reason": "Trade name of the same riders club."}
    stubber.add_response("converse", tool_reply("NameJudgment", verdict))
    result, calls, trajectory = research.research(client, form(name="VCR Snowmobile Club"))
    assert result.name_matches_form is True and result.name_explanation == verdict["reason"]
    assert [c.model for c in calls] == ["haiku"] and trajectory == []


def test_a_different_organization_is_investigated_by_the_agent(api, stubbed):
    client, stubber = stubbed
    stubber.add_response("converse", tool_reply("NameJudgment", {"same_organization": False, "reason": "Different."}))
    stubber.add_response("converse", tool_reply("search_organizations", {"query": "Other Club"}, "s1"))
    # The second agent turn must carry the search result back, matched to the call, with the name marked.
    finding = {"form_name_belongs_to": "12-3456789", "explanation": "The form's name is OTHER CLUB's (WI)."}
    expected_result = {"toolUseId": "s1", "content": [{"json": ANY}], "status": "success"}
    stubber.add_response(
        "converse",
        tool_reply("NameInvestigation", finding, "f1"),
        expected_params={
            "modelId": "us.anthropic.claude-sonnet-4-6",
            "system": ANY,
            "messages": [ANY, ANY, {"role": "user", "content": [{"toolResult": expected_result}]}],
            "inferenceConfig": ANY,
            "toolConfig": ANY,
        },
    )
    result, calls, trajectory = research.research(client, form(name="Other Club"))
    assert result.name_matches_form is False and result.form_name_belongs_to == "12-3456789"
    assert [c.model for c in calls] == ["haiku", "sonnet", "sonnet"]
    assert trajectory[0] == {"tool": "search_organizations", "input": {"query": "Other Club"}, "status": "success"}
    assert ("search", "Other Club") in api


def test_tool_results_mark_organization_names_as_unverified(api):
    found = propublica_tools.search_organizations("Other Club")
    assert found["organizations"][0]["name"] == {"unverified_text": "OTHER CLUB"}
    assert propublica_tools.get_organization("41-1657792")["name"] == {"unverified_text": "VALLEY CREST RIDERS INC"}


def test_a_failing_tool_reports_the_error_to_the_model(api, stubbed):
    client, stubber = stubbed
    stubber.add_response("converse", tool_reply("get_organization", {"ein": "not-an-ein"}, "g1"))
    stubber.add_response(
        "converse", tool_reply("NameInvestigation", {"form_name_belongs_to": None, "explanation": "?"})
    )
    _, _, trajectory = bedrock.tool_loop(
        client,
        step="t",
        model="sonnet",
        system="s",
        content=[{"text": "q"}],
        tools=propublica_tools.TOOLS,
        tool_specs=propublica_tools.TOOL_SPECS,
        finish=research.NameInvestigation,
        max_tokens=500,
        tool_errors=(propublica_tools.ToolError,),
    )
    assert trajectory[0]["status"] == "error"


def test_the_last_allowed_turn_forces_the_finish_tool(stubbed):
    client, stubber = stubbed
    stubber.add_response(
        "converse",
        tool_reply("NameInvestigation", {"form_name_belongs_to": None, "explanation": "Out of turns."}),
        expected_params={
            "modelId": ANY,
            "system": ANY,
            "messages": ANY,
            "inferenceConfig": ANY,
            "toolConfig": {"tools": ANY, "toolChoice": {"tool": {"name": "NameInvestigation"}}},
        },
    )
    answer, _, trajectory = bedrock.tool_loop(
        client,
        step="t",
        model="sonnet",
        system="s",
        content=[{"text": "q"}],
        tools={},
        tool_specs={},
        finish=research.NameInvestigation,
        max_tokens=500,
        max_turns=1,
    )
    assert answer.explanation == "Out of turns." and trajectory == [{"tool": "NameInvestigation", "forced": True}]


def test_the_agent_sees_names_as_unverified_text():
    payload = json.loads(research._names("Form Name", "IRS NAME", "411657792")[0]["text"])
    assert payload["name_on_application_form"] == {"unverified_text": "Form Name"}
    assert payload["ein"] == "41-1657792"
