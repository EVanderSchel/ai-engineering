"""The agent loop, with a fake Claude client that replays scripted responses: no API calls, no network."""

import json
from types import SimpleNamespace

import pytest

import agent
import tools


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, tool_input, id_):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=id_)


def response(stop_reason, *content, input_tokens=1000, output_tokens=100):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), usage=usage)


class FakeClient:
    """Returns the scripted responses in order (repeating the last one) and records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        self.requests.append(request | {"messages": list(request["messages"])})  # snapshot: the loop appends
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


@pytest.fixture(autouse=True)
def fake_tools(monkeypatch):
    def get_financials(ein, tax_year=None):
        if tax_year == 1999:
            raise tools.ToolError("No financial data for tax year 1999.")
        return {"ein": ein, "filings": [{"tax_year": tax_year or 2023, "total_revenue": 100}]}

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_financials", get_financials)
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "search_organizations", lambda query, state=None, page=0: {"q": query})


def test_answer_without_tools():
    client = FakeClient(response("end_turn", text("Hello.")))
    result = agent.run(client, "hi")
    assert (result.answer, result.stop, result.steps) == ("Hello.", "answered", 1)
    assert client.requests[0]["tools"] == agent.TOOL_SPECS


def test_tool_call_then_answer():
    client = FakeClient(
        response("tool_use", text("Looking it up."), tool_use("get_financials", {"ein": "53-0196605"}, "t1")),
        response("end_turn", text("Revenue was $100.")),
    )
    result = agent.run(client, "revenue?")
    assert result.answer == "Revenue was $100."
    assert result.steps == 2
    assert [e["type"] for e in result.trajectory] == ["text", "tool", "text"]

    # The second request carries the whole conversation: question, Claude's tool request, and our result.
    second = client.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    [tool_result] = second[2]["content"]
    assert tool_result["tool_use_id"] == "t1"
    assert tool_result["is_error"] is False
    assert json.loads(tool_result["content"])["filings"][0]["total_revenue"] == 100


def test_parallel_tool_calls_return_together():
    client = FakeClient(
        response(
            "tool_use",
            tool_use("get_financials", {"ein": "1"}, "a"),
            tool_use("get_financials", {"ein": "2"}, "b"),
        ),
        response("end_turn", text("Done.")),
    )
    agent.run(client, "compare")
    results = client.requests[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


@pytest.mark.parametrize(
    ("name", "tool_input", "message"),
    [
        ("get_financials", {"ein": "1", "tax_year": 1999}, "No financial data for tax year 1999."),  # ToolError
        ("get_financials", {"eni": "1"}, "Invalid arguments for get_financials"),  # misspelled argument
        ("delete_everything", {}, "Unknown tool 'delete_everything'"),
    ],
)
def test_tool_failures_go_back_to_claude_as_errors(name, tool_input, message):
    client = FakeClient(response("tool_use", tool_use(name, tool_input, "t1")), response("end_turn", text("Sorry.")))
    result = agent.run(client, "q")
    [tool_result] = client.requests[1]["messages"][2]["content"]
    assert tool_result["is_error"] is True
    assert message in tool_result["content"]
    assert result.stop == "answered"


def test_unexpected_exception_is_reported_not_raised(monkeypatch):
    def broken(**kwargs):
        raise ConnectionError("down")

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_financials", broken)
    output, is_error = agent.run_tool("get_financials", {"ein": "1"})
    assert is_error and "ConnectionError" in output


def test_stops_at_max_steps():
    client = FakeClient(response("tool_use", tool_use("search_organizations", {"query": "x"}, "t")))
    result = agent.run(client, "loop forever", max_steps=3)
    assert (result.stop, result.steps, result.answer) == ("max_steps", 3, None)


def test_stops_at_max_cost():
    expensive = response("tool_use", tool_use("search_organizations", {"query": "x"}, "t"), input_tokens=200_000)
    result = agent.run(FakeClient(expensive), "q", max_cost=0.25)  # 200k Sonnet input tokens = $0.40
    assert (result.stop, result.steps) == ("max_cost", 1)


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_other_stop_reasons_end_the_run_without_an_answer(stop_reason):
    result = agent.run(FakeClient(response(stop_reason)), "q")
    assert (result.stop, result.answer) == (stop_reason, None)


def test_cost_counts_cache_reads_and_writes_at_their_prices():
    usage = agent.Usage(
        input_tokens=1_000_000, cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000
    )
    assert usage.cost("claude-sonnet-5") == pytest.approx(2.00 + 2.50 + 0.20)


def test_every_tool_spec_has_a_function():
    assert {spec["name"] for spec in agent.TOOL_SPECS} == set(agent.TOOL_FUNCTIONS)
