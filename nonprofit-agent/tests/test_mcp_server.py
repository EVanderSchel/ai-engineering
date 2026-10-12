"""The MCP server, through a real MCP client: in-process for tool calls (with fake tools, no network), and
once as a subprocess over stdio, the way Claude Code or Claude Desktop starts it."""

import asyncio
import json
import sys

import pytest
from mcp import Client, StdioServerParameters

import agent
import mcp_server
import paths
import tools


def call(coroutine_fn):
    """Run an async check against the server, with a connected client."""

    async def main():
        async with Client(mcp_server.server) as client:
            return await coroutine_fn(client)

    return asyncio.run(main())


@pytest.fixture(autouse=True)
def fake_tools(monkeypatch):
    def get_financials(ein, tax_year=None):
        if tax_year == 1999:
            raise tools.ToolError("No financial data for tax year 1999.")
        return {"ein": ein, "filings": [{"tax_year": tax_year or 2023, "total_revenue": 100}]}

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_financials", get_financials)


def test_clients_see_the_agents_tool_specs():
    listed = {t.name: t for t in call(lambda c: c.list_tools()).tools}
    assert set(listed) == {spec["name"] for spec in agent.TOOL_SPECS}
    for spec in agent.TOOL_SPECS:
        tool = listed[spec["name"]]
        assert tool.description == spec["description"]
        assert set(tool.input_schema["required"]) == set(spec["input_schema"]["required"])
        for param, about in spec["input_schema"]["properties"].items():
            assert tool.input_schema["properties"][param]["description"] == about["description"]
        assert tool.annotations.read_only_hint is True


def test_a_call_returns_what_the_agent_loop_gets():
    result = call(lambda c: c.call_tool("get_financials", {"ein": "1", "tax_year": 2022}))
    assert result.is_error is False
    assert result.content[0].text == agent.run_tool("get_financials", {"ein": "1", "tax_year": 2022})[0]
    assert json.loads(result.content[0].text)["filings"][0]["tax_year"] == 2022


def test_tool_errors_come_back_as_error_results_with_the_same_message():
    result = call(lambda c: c.call_tool("get_financials", {"ein": "1", "tax_year": 1999}))
    assert result.is_error is True
    # The MCP SDK adds which tool failed; the agent's own message follows unchanged.
    assert result.content[0].text == "Error executing tool get_financials: No financial data for tax year 1999."


def test_invalid_arguments_are_rejected_before_the_tool_runs():
    result = call(lambda c: c.call_tool("get_financials", {"eni": "1"}))  # misspelled, and ein missing
    assert result.is_error is True
    assert "ein" in result.content[0].text


def test_the_system_prompt_travels_as_server_instructions():
    async def instructions(client):
        return client.instructions

    assert call(instructions) == agent.SYSTEM_PROMPT


def test_starts_over_stdio_and_lists_tools():
    """A real subprocess, as an MCP client launches it. Fails if the server can't start or writes anything
    but protocol messages to stdout."""
    params = StdioServerParameters(command=sys.executable, args=[str(paths.ROOT / "src" / "mcp_server.py")])

    async def main():
        async with Client(params) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert sorted(asyncio.run(main())) == sorted(spec["name"] for spec in agent.TOOL_SPECS)
