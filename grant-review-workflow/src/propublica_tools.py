"""ProPublica lookups for research: the fixed lookup by EIN (plain code), and the two tools the research agent
may call. Trimmed from Project 3's tools.py.

Tool results keep only useful fields with plain names (every byte is input tokens the model pays for), and
organization names are wrapped as {"unverified_text": ...}: a name is text the organization chose, and it
reaches the model, so it's marked as data to read, never instructions to follow (Project 3's injection
defense). When a request can't be served, a tool raises ToolError with a message written for the model.
"""

import re

import propublica

FORM_NAMES = {0: "990", 1: "990-EZ", 2: "990-PF"}


class ToolError(Exception):
    """A request the tool can't serve; the message is shown to the model."""


def normalize_ein(ein: str | int) -> str:
    digits = re.sub(r"[\s-]", "", str(ein))
    if not digits.isdigit() or len(digits) > 9:
        raise ToolError(f"{ein!r} is not an EIN (9 digits, e.g. 53-0196605). Use search_organizations to find one.")
    return digits.zfill(9)


def format_ein(ein: str | int) -> str:
    digits = normalize_ein(ein)
    return f"{digits[:2]}-{digits[2:]}"


def untrusted(text: str | None) -> dict | None:
    return None if text is None else {"unverified_text": text}


# --- The fixed lookup (code, no model) ---------------------------------------------------------------


def lookup(ein: str) -> dict | None:
    """The public record for an EIN, or None if ProPublica has no such organization: its registered name,
    which tax years have figures, and the latest year's revenue and expenses."""
    try:
        data = propublica.organization(normalize_ein(ein))
    except propublica.NotFound:
        return None
    filings = sorted(data["filings_with_data"], key=lambda f: f["tax_prd"], reverse=True)
    latest = filings[0] if filings else {}
    return {
        "registered_name": data["organization"]["name"],
        "years_with_figures": sorted({f["tax_prd_yr"] for f in filings}, reverse=True),
        "latest_year": latest.get("tax_prd_yr"),
        "latest_revenue": latest.get("totrevenue"),
        "latest_expenses": latest.get("totfuncexpns"),
    }


# --- The research agent's tools --------------------------------------------------------------------


def search_organizations(query: str, state: str | None = None) -> dict:
    """Find tax-exempt organizations by name, optionally in one US state (2-letter code). First 25 matches."""
    if state is not None and not re.fullmatch(r"[A-Za-z]{2}", state):
        raise ToolError(f"state must be a 2-letter code like 'IA', not {state!r}.")
    try:
        data = propublica.search(query, state=state.upper() if state else None)
    except propublica.NotFound:  # a search with no matches is a 404
        return {"total_results": 0, "organizations": [], "hint": "No matches. Try fewer words or another spelling."}
    return {
        "total_results": data["total_results"],
        "organizations": [
            {"ein": format_ein(o["ein"]), "name": untrusted(o["name"]), "city": o["city"], "state": o["state"]}
            for o in data["organizations"]
        ],
    }


def get_organization(ein: str) -> dict:
    """An organization's registered name, location, and which tax years have figures."""
    try:
        data = propublica.organization(normalize_ein(ein))
    except propublica.NotFound:
        raise ToolError(f"No organization with EIN {format_ein(ein)}. Use search_organizations.") from None
    org = data["organization"]
    return {
        "ein": format_ein(org["ein"]),
        "name": untrusted(org["name"]),
        "city": org["city"],
        "state": org["state"],
        "years_with_figures": sorted({f["tax_prd_yr"] for f in data["filings_with_data"]}, reverse=True),
    }


TOOL_SPECS = {
    "search_organizations": {
        "description": search_organizations.__doc__,
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name or words in the name"},
                    "state": {"type": "string", "description": "Optional 2-letter state code"},
                },
                "required": ["query"],
            }
        },
    },
    "get_organization": {
        "description": get_organization.__doc__,
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {"ein": {"type": "string", "description": "9 digits, with or without the dash"}},
                "required": ["ein"],
            }
        },
    },
}
TOOLS = {"search_organizations": search_organizations, "get_organization": get_organization}
