"""Step 5: the agent's four tools as an MCP server, so any MCP client (Claude Code, Claude Desktop, another
agent) can research nonprofits with them, without this project's agent loop.

MCP (Model Context Protocol) is a standard for an app to discover a server's tools (names, descriptions,
input schemas) and call them. The server runs as a subprocess and talks JSON-RPC over stdin/stdout, so it
must never print to stdout (logging goes to stderr).

What a client gets is exactly what our agent gets: the same tool names and descriptions (agent.TOOL_SPECS),
the same parameter descriptions, and the same results and error messages (agent.run_tool). The system prompt
travels as the server's instructions, which clients can pass on to their model. What does NOT travel: the
step and cost limits and the grounding guardrail. Those live in our agent loop, so a client using these
tools directly gets the data and the rules, but not the checks.

All four tools only read public data, and are marked read-only (they change nothing) and open-world (they
call an outside API).

Run for an MCP client (stdio):  .venv\\Scripts\\python.exe src\\mcp_server.py
"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

import agent

SPECS = {spec["name"]: spec for spec in agent.TOOL_SPECS}
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

server = MCPServer(name="nonprofit-research", instructions=agent.SYSTEM_PROMPT)


def _about(tool: str, param: str) -> str:
    """A parameter's description, as the agent's own tool spec words it."""
    return SPECS[tool]["input_schema"]["properties"][param]["description"]


def _call(tool: str, **tool_input) -> str:
    """Run a tool exactly as the agent loop does; an error goes back to the client as an error result."""
    output, is_error = agent.run_tool(tool, tool_input)
    if is_error:
        raise ToolError(output)
    return output


def _register(name: str):
    return server.tool(
        name=name, description=SPECS[name]["description"], annotations=READ_ONLY, structured_output=False
    )


@_register("search_organizations")
def search_organizations(
    query: Annotated[str, Field(description=_about("search_organizations", "query"))],
    state: Annotated[str | None, Field(description=_about("search_organizations", "state"))] = None,
    page: Annotated[int, Field(description=_about("search_organizations", "page"))] = 0,
) -> str:
    return _call("search_organizations", query=query, state=state, page=page)


@_register("get_organization")
def get_organization(ein: Annotated[str, Field(description=_about("get_organization", "ein"))]) -> str:
    return _call("get_organization", ein=ein)


@_register("get_financials")
def get_financials(
    ein: Annotated[str, Field(description=_about("get_financials", "ein"))],
    tax_year: Annotated[int | None, Field(description=_about("get_financials", "tax_year"))] = None,
) -> str:
    return _call("get_financials", ein=ein, tax_year=tax_year)


@_register("compute_ratios")
def compute_ratios(
    ein: Annotated[str, Field(description=_about("compute_ratios", "ein"))],
    tax_year: Annotated[int, Field(description=_about("compute_ratios", "tax_year"))],
    compare_with_year: Annotated[int | None, Field(description=_about("compute_ratios", "compare_with_year"))] = None,
) -> str:
    return _call("compute_ratios", ein=ein, tax_year=tax_year, compare_with_year=compare_with_year)


if __name__ == "__main__":
    server.run("stdio")
