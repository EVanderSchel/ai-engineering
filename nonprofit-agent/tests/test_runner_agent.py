"""The Tool Runner version of the agent. The SDK's real runner runs here; only the HTTP layer is fake: each
request gets the next scripted API response, and every request body is recorded. No network, no API cost."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import agent
import runner_agent
import tools


def text(t):
    return {"type": "text", "text": t}


def tool_use(name, tool_input, id_):
    return {"type": "tool_use", "id": id_, "name": name, "input": tool_input}


def api_response(stop_reason, *content, input_tokens=1000, output_tokens=100):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class FakeAPI:
    """Serves scripted responses in order (repeating the last one) and records each request body."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        transport = httpx2.MockTransport(self.handle)
        self.client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=transport), max_retries=0)

    def handle(self, request):
        self.requests.append(json.loads(request.content))
        body = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return httpx2.Response(200, json=body)


@pytest.fixture(autouse=True)
def fake_tools(monkeypatch):
    def get_financials(ein, tax_year=None):
        if tax_year == 1999:
            raise tools.ToolError("No financial data for tax year 1999.")
        return {"ein": ein, "filings": [{"tax_year": tax_year or 2023, "total_revenue": 100}]}

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_financials", get_financials)


LOOKUP = api_response("tool_use", text("Looking it up."), tool_use("get_financials", {"ein": "1"}, "t1"))


def test_tool_call_then_answer():
    api = FakeAPI(LOOKUP, api_response("end_turn", text("Revenue was $100.")))
    result = runner_agent.run(api.client, "revenue?")
    assert (result.answer, result.stop, result.steps) == ("Revenue was $100.", "answered", 2)
    assert [e["type"] for e in result.trajectory] == ["text", "tool", "text"]
    follow_up = api.requests[1]
    assert "thread" not in follow_up  # no server-held history: the whole conversation is resent, as agent.run does
    assert [m["role"] for m in follow_up["messages"]] == ["user", "assistant", "user"]
    assert follow_up["system"][0]["text"] == agent.SYSTEM_PROMPT
    [tool_result] = follow_up["messages"][2]["content"]
    assert tool_result["tool_use_id"] == "t1"
    assert json.loads(tool_result["content"])["filings"][0]["total_revenue"] == 100


def test_the_request_matches_the_hand_built_loop():
    api = FakeAPI(api_response("end_turn", text("Hello.")))
    runner_agent.run(api.client, "hi")
    request = api.requests[0]
    assert request["model"] == agent.MODEL
    assert request["system"][0]["text"] == agent.SYSTEM_PROMPT
    assert request["cache_control"] == {"type": "ephemeral"}
    sent = {t["name"]: t for t in request["tools"]}
    for spec in agent.TOOL_SPECS:  # same descriptions and schemas, so only the loop differs
        assert sent[spec["name"]]["description"] == spec["description"]
        assert sent[spec["name"]]["input_schema"] == spec["input_schema"]


def test_tool_errors_reach_the_model_as_the_hand_built_loop_sends_them():
    api = FakeAPI(
        api_response("tool_use", tool_use("get_financials", {"ein": "1", "tax_year": 1999}, "t1")),
        api_response("end_turn", text("Sorry.")),
    )
    runner_agent.run(api.client, "q")
    [tool_result] = api.requests[1]["messages"][2]["content"]
    assert tool_result["is_error"] is True
    sent = runner_agent._text(tool_result["content"])
    assert sent == agent.run_tool("get_financials", {"ein": "1", "tax_year": 1999})[0]  # not repr(exc)


def test_an_ungrounded_draft_is_sent_back_with_a_new_runner():
    api = FakeAPI(
        LOOKUP,
        api_response("end_turn", text("Revenue was $999,999.")),
        api_response("end_turn", text("Revenue was $100.")),
    )
    result = runner_agent.run(api.client, "q")
    assert (result.answer, result.grounding_retries, result.ungrounded) == ("Revenue was $100.", 1, [])
    retry = api.requests[2]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user", "assistant", "user"]
    assert "$999,999" in retry[-1]["content"]


def test_stops_at_max_steps():
    api = FakeAPI(LOOKUP)  # always asks for another tool
    result = runner_agent.run(api.client, "q", max_steps=3)
    assert (result.stop, result.steps, result.answer) == ("max_steps", 3, None)
    assert len(api.requests) == 3


def test_stops_at_max_cost():
    api = FakeAPI(api_response("tool_use", tool_use("get_financials", {"ein": "1"}, "t1"), input_tokens=200_000))
    result = runner_agent.run(api.client, "q", max_cost=0.25)  # 200k Sonnet input tokens = $0.40
    assert (result.stop, result.steps) == ("max_cost", 1)
    assert len(api.requests) == 1


def test_both_loops_give_the_same_trajectory_and_answer():
    """The same scripted conversation through the hand-built loop and the runner."""
    script = [
        LOOKUP,
        api_response("tool_use", tool_use("get_financials", {"ein": "1", "tax_year": 1999}, "t2")),
        api_response("end_turn", text("Revenue was $999,999.")),
        api_response("end_turn", text("Revenue was $100.")),
    ]
    runner_result = runner_agent.run(FakeAPI(*script).client, "q")

    def as_object(body):  # the hand-built loop's fake client returns SDK-like objects
        content = [SimpleNamespace(**block) for block in body["content"]]
        return SimpleNamespace(
            stop_reason=body["stop_reason"],
            content=content,
            usage=SimpleNamespace(**body["usage"]),
            model=body["model"],
        )

    responses = [as_object(body) for body in script]
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **request: responses.pop(0)))
    loop_result = agent.run(fake, "q")

    assert runner_result.trajectory == loop_result.trajectory
    assert (runner_result.answer, runner_result.steps, runner_result.grounding_retries) == (
        loop_result.answer,
        loop_result.steps,
        loop_result.grounding_retries,
    )


def test_an_api_error_stops_the_runner_like_the_hand_built_loop():
    class FailingAPI(FakeAPI):
        def handle(self, request):
            self.requests.append(json.loads(request.content))
            if len(self.requests) == 1:
                return httpx2.Response(200, json=LOOKUP)
            return httpx2.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Your credit balance is too low to access the Anthropic API.",
                    },
                },
            )

    result = runner_agent.run(FailingAPI(LOOKUP).client, "q")
    assert result.stop == "api_error" and "credit balance" in result.error
    assert [e["type"] for e in result.trajectory] == ["text", "tool"]
